//! Floating point exactly as CPython 3.12 on Windows does it, so the port's output matches
//! the Python app's to the bit.
//!
//! - Transcendentals go to the same C runtime functions CPython's math module calls (ucrt),
//!   through a pointer the optimiser cannot see through: LLVM would otherwise fold or rewrite
//!   some calls (pow(x, 2.0) into x * x, pow(x, 0.5) into sqrt), which can differ in the
//!   last bit.
//! - `**`, `%`, `sum()`, `math.hypot`, `max`/`min` and complex arithmetic follow CPython's
//!   own code, which is not what Rust's operators or methods do.

use std::hint::black_box;

mod crt {
    extern "C" {
        pub fn sin(x: f64) -> f64;
        pub fn cos(x: f64) -> f64;
        pub fn exp(x: f64) -> f64;
        pub fn log(x: f64) -> f64;
        pub fn log10(x: f64) -> f64;
        pub fn tanh(x: f64) -> f64;
        pub fn pow(x: f64, y: f64) -> f64;
        #[cfg_attr(windows, link_name = "_hypot")]
        pub fn hypot(x: f64, y: f64) -> f64;
    }
}

type Unary = unsafe extern "C" fn(f64) -> f64;
type Binary = unsafe extern "C" fn(f64, f64) -> f64;

#[inline]
fn unary(f: Unary, x: f64) -> f64 {
    // SAFETY: the C runtime's math functions take and return plain doubles.
    unsafe { black_box(f)(x) }
}

#[inline]
fn binary(f: Binary, x: f64, y: f64) -> f64 {
    // SAFETY: as above.
    unsafe { black_box(f)(x, y) }
}

pub const PI: f64 = std::f64::consts::PI;
pub const E: f64 = std::f64::consts::E;

pub fn sin(x: f64) -> f64 {
    unary(crt::sin, x)
}

pub fn cos(x: f64) -> f64 {
    unary(crt::cos, x)
}

pub fn exp(x: f64) -> f64 {
    unary(crt::exp, x)
}

/// math.log of a positive number (CPython's m_log calls log() for finite positive x).
pub fn log(x: f64) -> f64 {
    if x.is_finite() && x > 0.0 {
        unary(crt::log, x)
    } else if x.is_nan() || x == f64::INFINITY {
        x
    } else {
        f64::NAN // math.log raises; nothing here passes it 0 or less
    }
}

pub fn log10(x: f64) -> f64 {
    if x.is_finite() && x > 0.0 {
        unary(crt::log10, x)
    } else if x.is_nan() || x == f64::INFINITY {
        x
    } else {
        f64::NAN
    }
}

pub fn tanh(x: f64) -> f64 {
    unary(crt::tanh, x)
}

/// sqrt is correctly rounded everywhere; here for symmetry.
pub fn sqrt(x: f64) -> f64 {
    x.sqrt()
}

/// abs(complex): CPython calls the C library's hypot.
pub fn c_hypot(x: f64, y: f64) -> f64 {
    binary(crt::hypot, x, y)
}

fn is_odd_integer(x: f64) -> bool {
    // DOUBLE_IS_ODD_INTEGER: fmod(fabs(x), 2.0) == 1.0
    (x.abs() % 2.0) == 1.0
}

/// float ** float (CPython's float_pow, its special cases before the C pow).
pub fn pow(base: f64, exponent: f64) -> f64 {
    let (mut iv, iw) = (base, exponent);
    if iw == 0.0 {
        return 1.0;
    }
    if iv.is_nan() {
        return iv;
    }
    if iw.is_nan() {
        return if iv == 1.0 { 1.0 } else { iw };
    }
    if iw.is_infinite() {
        let a = iv.abs();
        return if a == 1.0 {
            1.0
        } else if (iw > 0.0) == (a > 1.0) {
            f64::INFINITY
        } else {
            0.0
        };
    }
    if iv.is_infinite() {
        let odd = is_odd_integer(iw);
        return if iw > 0.0 {
            if odd { iv } else { iv.abs() }
        } else if odd {
            0.0f64.copysign(iv)
        } else {
            0.0
        };
    }
    if iv == 0.0 {
        // a negative power of zero raises in Python
        return if is_odd_integer(iw) { iv } else { 0.0 };
    }
    let mut negate = false;
    if iv < 0.0 {
        if iw != iw.floor() {
            return f64::NAN; // complex in Python; never happens here
        }
        iv = -iv;
        negate = is_odd_integer(iw);
    }
    if iv == 1.0 {
        return if negate { -1.0 } else { 1.0 };
    }
    let ix = binary(crt::pow, iv, iw);
    if negate {
        -ix
    } else {
        ix
    }
}

/// float % float: the remainder takes the divisor's sign (CPython's float_rem).
pub fn rem(x: f64, y: f64) -> f64 {
    let m = x % y; // fmod, exact
    if m != 0.0 {
        if (y < 0.0) != (m < 0.0) {
            m + y
        } else {
            m
        }
    } else {
        0.0f64.copysign(y)
    }
}

/// max(a, b): the first unless the second is larger.
#[inline]
pub fn max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// min(a, b): the first unless the second is smaller.
#[inline]
pub fn min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// Python's round() to an integer: halves go to the even neighbour.
pub fn round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// sum() of floats in CPython 3.12: Neumaier's compensated summation, started from the int 0.
pub fn sum<I: IntoIterator<Item = f64>>(items: I) -> f64 {
    let mut iter = items.into_iter();
    let Some(first) = iter.next() else {
        return 0.0;
    };
    let mut total = 0.0 + first;
    let mut c = 0.0;
    for x in iter {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

fn frexp_exponent(x: f64) -> i32 {
    // frexp's exponent: x = m * 2**e with 0.5 <= |m| < 1, for finite non-zero x
    let bits = x.abs().to_bits();
    let biased = ((bits >> 52) & 0x7ff) as i32;
    if biased == 0 {
        // subnormal: scale it into the normal range first
        frexp_exponent(x * f64::powi(2.0, 64)) - 64
    } else {
        biased - 1022
    }
}

fn ldexp_one(e: i32) -> f64 {
    // 2**e, exactly
    if e >= -1022 {
        f64::from_bits(((e + 1023) as u64) << 52)
    } else {
        f64::from_bits(1u64 << (52 + (e + 1022)))
    }
}

struct DoubleLength {
    hi: f64,
    lo: f64,
}

fn dl_fast_sum(a: f64, b: f64) -> DoubleLength {
    let x = a + b;
    let z = x - a;
    let y = b - z;
    DoubleLength { hi: x, lo: y }
}

fn dl_mul(x: f64, y: f64) -> DoubleLength {
    let z = x * y;
    let zz = x.mul_add(y, -z);
    DoubleLength { hi: z, lo: zz }
}

/// CPython 3.12's vector_norm, as math.hypot uses it.
fn vector_norm(vec: &mut [f64], max: f64, found_nan: bool) -> f64 {
    if max.is_infinite() {
        return max;
    }
    if found_nan {
        return f64::NAN;
    }
    if max == 0.0 || vec.len() <= 1 {
        return max;
    }
    let max_e = frexp_exponent(max);
    if max_e < -1023 {
        for v in vec.iter_mut() {
            *v /= f64::MIN_POSITIVE;
        }
        return f64::MIN_POSITIVE * vector_norm(vec, max / f64::MIN_POSITIVE, found_nan);
    }
    let scale = ldexp_one(-max_e);
    let (mut csum, mut frac1, mut frac2) = (1.0f64, 0.0f64, 0.0f64);
    for &v in vec.iter() {
        let x = v * scale;
        let pr = dl_mul(x, x);
        let sm = dl_fast_sum(csum, pr.hi);
        csum = sm.hi;
        frac1 += pr.lo;
        frac2 += sm.lo;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let pr = dl_mul(-h, h);
    let sm = dl_fast_sum(csum, pr.hi);
    csum = sm.hi;
    frac1 += pr.lo;
    frac2 += sm.lo;
    let x = csum - 1.0 + (frac1 + frac2);
    h += x / (2.0 * h);
    h / scale
}

/// math.hypot(x, y) (CPython's own, not the C library's).
pub fn hypot(x: f64, y: f64) -> f64 {
    let mut coordinates = [x.abs(), y.abs()];
    let mut max = 0.0;
    let mut found_nan = false;
    for &c in &coordinates {
        found_nan |= c.is_nan();
        if c > max {
            max = c;
        }
    }
    vector_norm(&mut coordinates, max, found_nan)
}

/// copysign as math.copysign.
pub fn copysign(x: f64, y: f64) -> f64 {
    x.copysign(y)
}

/// A complex number with CPython 3.12's arithmetic (a float operand becomes x + 0j first).
#[derive(Clone, Copy, Debug)]
pub struct Complex {
    pub re: f64,
    pub im: f64,
}

// add/mul/div by name: CPython's _Py_c_* functions, kept apart from Rust's operator traits
#[allow(clippy::should_implement_trait)]
impl Complex {
    pub fn real(x: f64) -> Complex {
        Complex { re: x, im: 0.0 }
    }

    pub fn add(self, b: Complex) -> Complex {
        Complex { re: self.re + b.re, im: self.im + b.im }
    }

    pub fn mul(self, b: Complex) -> Complex {
        Complex { re: self.re * b.re - self.im * b.im, im: self.re * b.im + self.im * b.re }
    }

    /// _Py_c_quot: Smith's algorithm.
    pub fn div(self, b: Complex) -> Complex {
        let abs_breal = b.re.abs();
        let abs_bimag = b.im.abs();
        if abs_breal >= abs_bimag {
            if abs_breal == 0.0 {
                Complex { re: 0.0, im: 0.0 }
            } else {
                let ratio = b.im / b.re;
                let denom = b.re + b.im * ratio;
                Complex { re: (self.re + self.im * ratio) / denom, im: (self.im - self.re * ratio) / denom }
            }
        } else if abs_bimag >= abs_breal {
            let ratio = b.re / b.im;
            let denom = b.re * ratio + b.im;
            Complex { re: (self.re * ratio + self.im) / denom, im: (self.im * ratio - self.re) / denom }
        } else {
            Complex { re: f64::NAN, im: f64::NAN }
        }
    }

    pub fn abs(self) -> f64 {
        c_hypot(self.re, self.im)
    }

    /// cmath.exp of a finite number.
    pub fn exp(self) -> Complex {
        let l = exp(self.re);
        Complex { re: l * cos(self.im), im: l * sin(self.im) }
    }
}

/// cmath.exp(-1j * TAU * freq / SAMPLE_RATE), the point on the unit circle a biquad's
/// magnitude is read at, built as Python builds it.
pub fn unit_circle(tau: f64, freq: f64, sample_rate: f64) -> Complex {
    let minus_j = Complex { re: -0.0, im: -1.0 };
    minus_j.mul(Complex::real(tau)).mul(Complex::real(freq)).div(Complex::real(sample_rate)).exp()
}

/// |(b0 + b1 z + b2 z^2) / (1 + a1 z + a2 z^2)| at freq, as the Python filters' magnitude().
pub fn biquad_magnitude(b: [f64; 3], a: [f64; 2], tau: f64, freq: f64, sample_rate: f64) -> f64 {
    let z = unit_circle(tau, freq, sample_rate);
    let num = Complex::real(b[0])
        .add(Complex::real(b[1]).mul(z))
        .add(Complex::real(b[2]).mul(z).mul(z));
    let den = Complex::real(1.0)
        .add(Complex::real(a[0]).mul(z))
        .add(Complex::real(a[1]).mul(z).mul(z));
    num.div(den).abs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sum_compensates() {
        assert_eq!(sum([0.1, 0.2, 0.3]), 0.6);
        assert_eq!(sum([1e100, 1.0, -1e100]), 1.0);
        assert_eq!(sum(std::iter::empty()), 0.0);
    }

    #[test]
    fn rem_takes_the_divisor_sign() {
        assert_eq!(rem(-0.15, 1.0), 1.0 - 0.15);
        assert_eq!(rem(1.25, 1.0), 0.25);
        assert_eq!(rem(-1e-20, 1.0), 1.0);
    }

    #[test]
    fn hypot_is_exact_on_easy_cases() {
        assert_eq!(hypot(3.0, 4.0), 5.0);
        assert_eq!(hypot(0.0, -2.5), 2.5);
        assert_eq!(hypot(0.0, 0.0), 0.0);
    }

    #[test]
    fn pow_special_cases() {
        assert_eq!(pow(0.0, 0.7), 0.0);
        assert_eq!(pow(2.0, 0.0), 1.0);
        assert_eq!(pow(-2.0, 2.0), 4.0);
        assert_eq!(pow(-2.0, 3.0), -8.0);
    }

    #[test]
    fn round_is_half_even() {
        assert_eq!(round_int(0.5), 0);
        assert_eq!(round_int(1.5), 2);
        assert_eq!(round_int(-2.5), -2);
        assert_eq!(round_int(70.00000000000001), 70);
    }
}
