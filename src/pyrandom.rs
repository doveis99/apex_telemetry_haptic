//! Python's `random.Random`: MT19937 seeded and drawn from exactly as CPython does, so a seed
//! gives the port the same noise, the same rattles and the same firings as the Python app.

const N: usize = 624;
const M: usize = 397;
const MATRIX_A: u32 = 0x9908_b0df;
const UPPER_MASK: u32 = 0x8000_0000;
const LOWER_MASK: u32 = 0x7fff_ffff;

#[derive(Clone)]
pub struct PyRandom {
    mt: [u32; N],
    index: usize,
}

impl PyRandom {
    /// random.Random(seed) for a non-negative integer seed.
    pub fn new(seed: u64) -> PyRandom {
        let mut rng = PyRandom { mt: [0; N], index: N + 1 };
        // CPython splits abs(seed) into 32-bit words, least significant first, at least one.
        let mut key = vec![seed as u32];
        if seed >> 32 != 0 {
            key.push((seed >> 32) as u32);
        }
        rng.init_by_array(&key);
        rng
    }

    /// Seeded from the clock and the OS's hash keys, as the global random module is from os.urandom.
    pub fn from_entropy() -> PyRandom {
        use std::collections::hash_map::RandomState;
        use std::hash::{BuildHasher, Hasher};
        let mut hasher = RandomState::new().build_hasher();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        hasher.write_u64(nanos);
        PyRandom::new(hasher.finish())
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..N {
            let prev = self.mt[i - 1];
            self.mt[i] = 1_812_433_253u32.wrapping_mul(prev ^ (prev >> 30)).wrapping_add(i as u32);
        }
        self.index = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19_650_218);
        let (mut i, mut j) = (1usize, 0usize);
        let mut k = N.max(key.len());
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1_664_525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
            k -= 1;
        }
        k = N - 1;
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1_566_083_941)).wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            k -= 1;
        }
        self.mt[0] = 0x8000_0000;
        self.index = N;
    }

    fn genrand_u32(&mut self) -> u32 {
        if self.index >= N {
            for kk in 0..N {
                let y = (self.mt[kk] & UPPER_MASK) | (self.mt[(kk + 1) % N] & LOWER_MASK);
                let mag = if y & 1 != 0 { MATRIX_A } else { 0 };
                self.mt[kk] = self.mt[(kk + M) % N] ^ (y >> 1) ^ mag;
            }
            self.index = 0;
        }
        let mut y = self.mt[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^ (y >> 18)
    }

    /// random(): 53 random bits in [0, 1).
    pub fn random(&mut self) -> f64 {
        let a = self.genrand_u32() >> 5;
        let b = self.genrand_u32() >> 6;
        (a as f64 * 67_108_864.0 + b as f64) * (1.0 / 9_007_199_254_740_992.0)
    }

    /// uniform(a, b) = a + (b - a) * random()
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    fn getrandbits(&mut self, k: u32) -> u32 {
        debug_assert!(k > 0 && k <= 32);
        self.genrand_u32() >> (32 - k)
    }

    /// _randbelow(n) for 0 < n <= 2**32 - 1, by rejection as CPython does.
    fn randbelow(&mut self, n: u32) -> u32 {
        let k = 32 - n.leading_zeros();
        let mut r = self.getrandbits(k);
        while r >= n {
            r = self.getrandbits(k);
        }
        r
    }

    /// randrange(n)
    pub fn randrange(&mut self, n: u32) -> u32 {
        self.randbelow(n)
    }

    /// choice(items)
    pub fn choice<T: Copy>(&mut self, items: &[T]) -> T {
        items[self.randbelow(items.len() as u32) as usize]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Values from CPython 3.12: random.Random(seed).random() etc.
    #[test]
    fn matches_cpython() {
        let mut r = PyRandom::new(7);
        assert_eq!(r.random(), 0.32383276483316237);
        assert_eq!(r.uniform(-1.0, 1.0), -0.6983016521509962);
        let mut r = PyRandom::new(0);
        assert_eq!(r.random(), 0.8444218515250481);
        let mut r = PyRandom::new(20261108);
        let draws: Vec<u32> = (0..5).map(|_| r.randrange(1 << 30)).collect();
        assert_eq!(draws, [703054138, 967594050, 479082200, 640385891, 828295202]);
        let mut r = PyRandom::new(1);
        let picks: Vec<f64> = (0..8).map(|_| r.choice(&[-1.0, 1.0])).collect();
        assert_eq!(picks, [-1.0, -1.0, 1.0, -1.0, 1.0, 1.0, 1.0, 1.0]);
        let mut r = PyRandom::new((1 << 40) + 5);
        assert_eq!(r.random(), 0.5043802970418443);
    }
}
