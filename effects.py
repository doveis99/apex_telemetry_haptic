"""Turn telemetry frames into 1 kHz waveforms for the APEX 6 Pro.

Outputs: left grip, right grip and one trigger channel, which plays on the
left trigger, the right or both (one waveform either way). Each effect is
routed to the grips, the trigger or both (Gains.routes); on the trigger, what
belongs to one pedal plays on its own trigger (BRAKE_TRIGGER,
THROTTLE_TRIGGER). Telemetry is read once per 8 ms
block; amplitudes are ramped across the block and oscillator phases carry
over, so parameter changes do not click.

Resonances as the motor calibration felt them on the developer's pad: grips
65 Hz, triggers about 100 Hz (Flydigi's engine presets say 65 and 95).
Effects are placed near those to get the most feel per unit of drive.
"""

from __future__ import annotations

import bisect
import cmath
import functools
import math
import random
from dataclasses import dataclass, field, replace

from apex6 import MAX_SAMPLE, SAMPLE_LIMIT, SAMPLE_RATE, SUBFRAMES, Block, Route
from i18n import N_, t
from telemetry import Frame, Wheel

DT = SUBFRAMES / SAMPLE_RATE  # 8 ms per telemetry update
# Where each channel's soft limit tops out; its slope stays MAX_SAMPLE, so
# only the loudest peaks change. Grips get the protocol's whole range, as
# Flydigi's engine gives them (no grip ceiling in its composer, captured at
# 122). Triggers stop at 0.8 of it, the composer's trigger ceiling in xHaptics
# Engine 5.0.2.6. Both replace ASB's flat 112, which no pad decided.
CEILING = {"grip": SAMPLE_LIMIT, "trigger": round(0.8 * SAMPLE_LIMIT)}
TAU = 2.0 * math.pi
# Steady fade: a vibration that holds still soon goes unnoticed and only tires
# the hand, so what the continuous effects tell lies in how they change. Engine,
# road and trigger come back in full the moment what drives them moves, then
# settle over tens of seconds toward a floor (Gains.steady_fade) while it holds.
# Moves inside a dead band do not count: cruise control holding its speed, a
# finger resting on the trigger. The fade used to follow every such wobble back
# within a few seconds, which on the game's scaled-down roads left the engine
# and trigger near 90%, and it left out the road, nearly all of the grips' bed
# at speed (80 km/h on asphalt: road 12.9 RMS, engine 1.0).
CHANGE_TAU = 2.0  # s for the reference to catch up with an input that stopped moving
CHANGE_K = 1.0 - math.exp(-DT / CHANGE_TAU)
FADE_TAU = 8.0  # s; two thirds of the way to the floor 8 s after the last change, nearly there by 25
FADE_K = math.exp(-DT / FADE_TAU)
STEADY_BEDS = ("engine", "road", "trigger")
# The plugin rewrites shared memory once per rendered game frame, so at 60 fps
# about every other block reads the frame it read before. Suspension rates are
# taken over the game time between new frames (Frame.render_time), not over a
# block: dividing by DT made each change look up to 125/fps times faster, and
# at 30 fps the bump pulses never stopped ringing.
MAX_FRAME_GAP = 0.2  # s; across a longer hitch, or the game's timer restarting, only take a new reference
RATE_AVG_TAU = 0.2  # s, the slow suspension movement that bumps stand out from
ROUGH_TAU = 0.076  # s, smoothing of the road roughness estimate
# How rough the ground under the truck is, from its own suspension: the median at 10-30 km/h
# was 0.004 m/s on pavement and 0.071 on a dirt road, 0.017 on pavement from 60 km/h
# (2026-10-31 to 11-02). Taken in proportion and small (1.5 per m/s), it added 0.1 to the
# dirt and the surfaces' own strength, which falls with speed, ruled: a dirt road at its
# usual pace came out weaker than the highway (grips 11 against 18 of 112), and every road
# felt the same. ZAF's cab rattle grows with the square of the cab's acceleration, which was
# 4-8 times as large on that dirt road as on pavement; so does this, full at ROUGH_FULL.
ROUGH_FULL = 0.12  # m/s of roughness that alone gives the texture its full strength
ROUGH_POWER = 2.0
ROUGH_NOVELTY = 1.5  # per m/s of roughness, what the steady fade takes for a change of ground
# The game calls nearly all pavement "road" (93% of the time at speed in three drives, the rest
# mostly "static" for bridges and yards), so the surfaces' own textures hardly ever differ on a
# drive. What does differ is the road under the wheels: against its own last few seconds the
# roughness ran from half to 1.6 times (10th to 90th percentile, at any speed, both drives
# measured). The road follows that, by its square root (0.7-1.3 times), around the same level:
# a worn patch coarsens it, new asphalt smooths it.
DETAIL_TAU = 2.0  # s the roughness is held against
DETAIL_RANGE = (0.25, 4.0)  # of that ratio, before the root
# A locking wheel chops the brake trigger like ABS, timed per sample so every
# pulse is the same length (whole blocks gave 40 or 48 ms).
LOCK_RATE = 12.0  # Hz
LOCK_EDGE = 0.003  # s ramp at each edge: a crisp pulse without the click of a step
# Which way the game's yaw rate turns is read off the wheels (in a left turn the
# right ones run faster) rather than trusted from the SDK's axes: the sign may
# change once the turns summed to this much (rad/s times m/s of left/right
# spread), about a tenth of a second into a tight one.
YAW_EVIDENCE = 2.0
# Retarder: a smooth hum whose pitch follows the driveline. Engine brake: each
# cylinder letting its compression go knocks once per firing, a bark at the
# engine's firing rate that is gone under throttle.
RETARDER_HZ = (40.0, 65.0)  # standing, and from 90 km/h
JAKE_PEAK = 0.1  # of a firing cycle: the knock peaks this early, then the cylinder is quiet
_JAKE_MEAN = JAKE_PEAK * math.e * (1.0 - (1.0 + 1.0 / JAKE_PEAK) * math.exp(-1.0 / JAKE_PEAK))
# A diesel does not fire like a sine: its cylinders never fire quite alike, most
# unevenly at idle (a lope at half the firing rate and below), every firing
# differs from the last, and under load combustion turns harsh. The exhaust
# loops of two engine packs (Zeemods DD60 and Cummins X15) agree on the first
# two, within the band the grips play (20-250 Hz): from firing to firing the
# strength varies about 0.08 (std/mean), and a cylinder pattern of about 0.04
# stays at cruising revs and under load, where idle has about 0.06.
ENGINE_JITTER = 0.18  # spread of each firing's strength around its cylinder's
ENGINE_IMBALANCE = 0.3  # spread between cylinders at idle
ENGINE_UNEVEN_FLOOR = 0.4  # share of it that stays at any load and revs
# A V8 is lumpy where the sixes run smooth. In the Zeemods CAT 3408 V8's exhaust loops
# at 1000-1800 rpm only 9% of 20-250 Hz is on the firing rate (91-94% for the DD60 and
# X15), the half orders below it sit 3 dB under it (32-36 dB for the sixes), and the
# cylinder pattern is 0.32 (median of 9 loops, 0.14-0.38) against 0.05-0.08. Its idle is
# the even one: 0.21, with the half orders 11-26 dB down. So with eight cylinders the
# pattern holds at any load and revs instead of growing towards idle. Measured the same
# way (strength in windows one firing long, which blur neighbours together), a spread of
# 0.5 between the cylinders comes out at 0.32 at 1100-1700 rpm, and the six's at 0.08,
# as for the sixes' recordings.
ENGINE_V8_PATTERN = 0.5  # spread between cylinders (std/mean) of an eight
ENGINE_HARSH = 0.25  # third harmonic at full load
# Standing still, the idling engine is all the cab gives, and a diesel at idle shakes it;
# rolling, the road takes over. Replaying a 40 min drive (2026-11-02) at the default engine
# gain, the grips got 4 of 112 at a standstill (median; 9 in town, 19 at highway speed):
# next to nothing. So the idle grows up to ENGINE_STANDING times its strength as the truck
# stops, not the revs on top of it: 10 standing, as strong as the town's bed (4 times
# came to 13, nearly the highway's).
ENGINE_STANDING = 3.0
ENGINE_STANDING_SPEED = 3.0  # m/s from which the engine is as strong as on the move


@dataclass(frozen=True)
class Surface:
    level: float  # relative texture strength
    pitch: float  # Hz, centre of the texture at speed; it drops towards PITCH_STANDING of this when slow
    stones: float = 0.0  # loose stones per metre that click under a tyre
    crunch: float = 0.0  # share of the texture that is those clicks
    q: float = 1.2  # of the hiss's band: under 1 a broad, even hiss, over 2 nearly a hum
    swell: float = 0.25  # standard deviation of the slow swell, as a share of the texture
    swell_hz: float = 4.0  # how fast it swells: low is the ground heaving, high a jostle
    seams: float = 0.0  # m between seams the tyre rolls over evenly (slab joints, planks, grating); 0 for none
    seam_hz: float = 90.0  # what each seam knocks at
    seam_q: float = 1.5  # low a dull thud, high a ringing bar
    seam_share: float = 0.0  # of the texture's strength that is the seams


# The hand barely tells 100 Hz noise from 130 Hz noise, so surfaces that differed
# only in pitch and strength all felt like one hiss, louder or softer. What it does
# tell apart is how the texture moves: a smooth or a heaving swell, a broad hiss or
# a hum, stones, and seams coming at a steady beat that follows the speed.
#
# Matched by substring, first match wins, so the specific road_* variants come
# before plain "road". Pitch, band and swell after Sound Fixes Pack's tyre loops,
# 20-250 Hz (what the grips play): asphalt a broad, even hiss (middle half of its
# energy 50-190 Hz, 100 ms strength varies 0.12-0.18); gravel a narrow body at
# 75-105 Hz that swells more (0.22-0.34); snow 90-200 Hz and the most impulsive
# (kurtosis 3.5-6, against 3 for asphalt); grass high and narrow, 190-230 Hz. The
# rest, which the pack has no loop for, after the ground itself: concrete has
# joints every 4.6 m (15 ft, US practice), a 5 Hz beat at 80 km/h; wood planks
# drum, 20 cm apart; a steel grating sings; dirt and grass heave slowly.
SURFACES = (
    ("road_smooth", Surface(0.2, 120.0, q=0.8, swell=0.1)),
    # chip seal: grit, many small stones
    ("road_coarse", Surface(0.45, 95.0, 6.0, 0.3, q=0.8, swell=0.15, swell_hz=6.0)),
    ("road_dirt", Surface(0.55, 70.0, 1.5, 0.35, q=1.5, swell=0.45, swell_hz=1.5)),
    ("ice", Surface(0.15, 160.0, q=0.8, swell=0.05)),
    ("snow", Surface(0.45, 110.0, 2.0, 0.3, q=1.0, swell=0.35, swell_hz=2.5)),
    ("rumble", Surface(0.3, 100.0, q=0.8, swell=0.12)),  # the strip itself is the rumble effect; the texture stays that of asphalt
    ("road", Surface(0.3, 100.0, q=0.8, swell=0.12)), ("asphalt", Surface(0.3, 100.0, q=0.8, swell=0.12)),
    ("concrete", Surface(0.35, 90.0, q=0.8, swell=0.12, seams=4.6, seam_hz=85.0, seam_share=0.4)),
    # Anything modelled rather than painted on the terrain: company yards, and bridges and
    # ramps taken at speed. It is still paved ground, not air; no joints, which a bridge would not have every 4.6 m.
    ("static", Surface(0.35, 90.0, q=0.8, swell=0.12)),
    ("wood", Surface(0.55, 75.0, q=1.0, swell=0.2, seams=0.2, seam_hz=80.0, seam_q=1.0, seam_share=0.6)),
    ("metal", Surface(0.6, 130.0, q=1.2, swell=0.08, seams=0.15, seam_hz=150.0, seam_q=6.0, seam_share=0.7)),
    ("dirt", Surface(0.75, 60.0, 1.5, 0.4, q=1.5, swell=0.45, swell_hz=1.5)),
    ("grass", Surface(0.7, 200.0, q=3.0, swell=0.45, swell_hz=1.5)),
    ("gravel", Surface(1.0, 85.0, 3.0, 0.6, q=2.5, swell=0.3, swell_hz=5.0)),
    ("rough", Surface(0.9, 70.0, 2.0, 0.4, q=1.5, swell=0.45, swell_hz=2.0)),
)
DEFAULT_SURFACE = Surface(0.4, 90.0, 0.5, 0.1)
PITCH_STANDING = 0.6  # share of a surface's pitch at walking pace; all of it from 90 km/h
# Recorded road noise swells and fades a few times a second (undulations, the
# tyres' own unevenness); a steady texture feels machine-made. How deep and how
# fast is each surface's own (Surface.swell, swell_hz).
SEAM_SPEED = 22.0  # m/s (80 km/h) where seams are as strong as Surface.seam_share says; faster they come denser
SEAM_JITTER = 0.05  # seams are never quite evenly laid
# On rough ground the cab rattles: short low knocks, many a second.
RATTLE_HZ = 55.0
RATTLE_RATE = 40.0  # knocks per second per side at full roughness
# From 0.04, which pavement passes only one moment in ten at highway speed, a dirt road at
# its usual pace rattles half the time (it began at 0.08, its median, and was full at 0.3).
RATTLE_ROUGH = (0.04, 0.15)  # m/s of roughness where the rattle starts, and where it is full
# Rumble strips: the game reports a "rumble_stripe" substance under a wheel.
# It flickers back to road between the grooves, so a short hold bridges the
# gaps. Grooves are milled about 30 cm apart: 83 Hz at 90 km/h.
RUMBLE_SPACING = 0.3  # m
RUMBLE_HOLD = 0.25  # s
# Sound Fixes Pack's rumble strip loops are nearly a sine at the groove rate, not a
# buzz: second and third harmonics 15 and 19 dB down (median of three loops), and a
# half-rate component 7 dB down that lands every other groove harder.
RUMBLE_SHAPE = (0.45, 0.17, 0.11)  # half rate, second, third harmonic, against the fundamental
RUMBLE_LEVEL = 1.078  # keeps the RMS of the clipped sine, tanh(2.5 sin), the strip used to be
# Trailer wheels reach the cab through the coupling: their bumps and strips
# follow the truck's own, softer.
TRAILER_FEEL = 0.5
# Bumps, after recordings of joints and potholes: a joint is a short knock
# around 50-65 Hz, a pothole a heavy low thump after which the cab rattles for
# a few tenths of a second. Below about 40 Hz the grips give little back, so
# the biggest hits stop there.
BUMP_HZ = (65.0, 40.0)  # smallest and biggest hit
BUMP_DECAY = (0.025, 0.06)  # s
TRAILER_BUMP_HZ = 10.0  # higher: the trailer's hits come through the coupling lighter
BUMP_RATTLE_FROM = 0.3  # hit size (0..1) from which the cab rattles after it
BUMP_RATTLE_S = 0.3
BUMP_RATTLE_MAX = 16  # knocks alive at once, all wheels together
BUMP_RESTRIKE_S = 0.02  # s: a stronger reading this soon is the same step peaking (the next frame from 50 fps)
BUMP_GAP_S = 0.06  # s before a wheel knocks again: a step's rebound and the next frames of rough ground wait
# Each truck rides its own way, and so does each chassis physics mod: at 50 km/h and more, a
# 40 min drive in another truck under a stiff "realistic" chassis mod (2026-11-02) moved the
# suspension 2.5-3 times as fast as a 49 min drive under, most likely, the same mod's flexible
# version (2026-10-31), every minute of both (99th percentile 0.14-0.21 against 0.04-0.10 m/s).
# At the same threshold the first knocked 35 times a minute and the other 5: a rattle, not bumps. So
# the threshold also sits a few times above a slow average of how the vehicle itself has been
# moving (as SimHub's ShakeIt calibrates its wheel effects to each car), never below
# Gains.bump_threshold. Truck and trailer learn apart; each starts over when its wheels change.
BUMP_RIDE_TAU = 20.0  # s of driving the average spans
BUMP_RIDE_SPEED = 3.0  # m/s; slower (and parked) the ride is not learnt, and what was learnt stays
BUMP_OVER_RIDE = 12.0  # threshold as a multiple of that average: 35 knocks a minute became 9, the other 4.6 stayed 4.4
# A dirt road is no ride to learn: the wheels move 3-10 times as much there, all the time, and
# its ruts are no bigger than the rest (99th percentile 0.3-0.5 m/s at the usual 15-28 km/h,
# 0.4-1.1 driven hard; 2026-11-02, about 4.5 times the average where pavement's is 2-4).
# Learnt, the threshold rose past every rut and the road went quiet; without it, it knocked
# over 20 times a second. Held at BUMP_RIDE_MAX, it knocks 1.4 times a second at the usual
# speed, a hard one every 3 s; the paved drives above never reach it. The raised threshold only
# picks which movements knock: how hard is still measured from Gains.bump_threshold, so a rut
# lands as hard on any truck.
BUMP_RIDE_MAX = 0.5  # m/s, as high as it goes
# The cab on its mounts is what the driver sits in, and its tilt acceleration (Frame.cabin_accel)
# tells a dirt road from pavement far better than the wheels do: in a 10 min drive without a
# trailer (2026-11-02) its square had a median 30-100 times as large on dirt, and passed 0.02
# (rad/s^2)^2 74 times a minute on dirt at 10-20 km/h but 18 times in 6 min of pavement, nearly
# all at one kerb. ZAF's cab rattle follows the same square. So the cab sways (CAB_SWAY_*) as it
# moves, and it rattles (Texture) on whichever is the larger of the wheels' roughness and the
# cab's own shaking.
# The cab rocks on its mounts: on dirt (two drives, 2026-11-02) half the tilt acceleration's
# power lay below 2 Hz, peaking at 1.8-2.1 Hz, and after a lurch it swung back and forth twice
# within 0.5 s, settling in 0.16 s (median; 0.35 s at the 90th percentile). A knock struck once
# cannot follow that, and band noise like the cab's recorded boom (2026-11-03: 26-62 Hz,
# swelling for 0.10-0.15 s) felt grainy in the hand, its strength wobbling about 40% at random.
# So one smooth tone follows the tilt acceleration frame by frame. It starts above pavement's
# own shaking (99th percentile 0.05 rad/s^2, where dirt's median is 0.05-0.06), is at a lurch
# (0.14, where a knock used to strike) as strong as the smallest knock was, and is full at the
# dirt road's 99th percentile.
CAB_SWAY_HZ = 45.0  # near the recorded boom's loudest pitch (41.5-47 Hz), where the grips still answer
CAB_SWAY = ((0.06, 0.0), (0.14, 0.25), (0.6, 1.0))  # rad/s^2 of tilt acceleration, and strength
CAB_SWAY_TAU = 0.025  # s: smooths the game's frame steps (18 ms), not the 2 Hz swing
# The sway is a roll: in the same drives 94-98% of the tilt acceleration at a lurch was about
# the roll axis, and it was positive when the left wheels had sprung more than the right (86-95%
# of the lurches where they differed most; a larger deflection is a compressed spring). So the
# tone leans to the side the roll pushes, the far grip getting CAB_FAR_SIDE of it, and crosses
# over as the cab rocks back; a pitch lands on both alike.
CAB_FAR_SIDE = 0.5
CAB_RATTLE = (0.003, 0.05)  # (rad/s^2)^2 of shaking where the cab's rattle starts (pavement's 99th percentile) and is full
CAB_SETTLE_TAU = 0.25  # s for the shaking the rattle follows to die away
# The lurches alone left the road's own strength alike: in a 40 min drive (2026-11-02) the
# grips' median was 22 of 112 on a dirt road below 30 km/h and on the highway. The cab's
# shaking (the square above, settling over CAB_SETTLE_TAU) had a median of 0.004-0.01 on that
# dirt road and 0.0004 on the highway, whose 90th percentile is 0.0014: 0.037 rad/s^2. So the
# road also grows with the cab's shaking, from CAB_TEXTURE[0] rad/s^2, by its full strength at
# CAB_TEXTURE[1], where the dirt road shook at 15-30 km/h: replayed, the dirt road's median came
# to 31-36 against the highway's 21-22, and pavement's moved by less than 1. Full at 0.24, its
# 90th percentile, it came only to 27-32.
CAB_TEXTURE = (0.04, 0.12)
# The game's wheel deflection now and then leaps 25-100 mm for a single frame
# and is back on the next one, on one wheel, at any speed: in a 49 min drive
# (2026-10-31) four in five bump knocks came from such a frame, and nearly
# every time the road bed came back in full after its steady fade. A real step
# stays, and a real joint or pothole passes from axle to axle; these did not.
# So the suspension is read one game frame late (about 17 ms at 60 fps): a
# jump the next frame takes back is not one, and that wheel reads as moving
# straight from the frame before to the frame after.
SPIKE_MIN = 0.004  # m in one frame, about the bump threshold at 50 fps: smaller moves are left as they are
SPIKE_BACK = 0.3  # the next frame within this share of the jump from where it started: it came back
# Now and then the game also sends one physics state on two or more render frames, every
# wheel to the last bit the same (1 in 200-500 frames at speed in that drive). A leap held
# over such a repeat passed for a step: in the same drive a front wheel stood at 59 mm for
# two frames at 35 km/h, came back to 5 on the third and knocked at full strength. So a
# repeat is not a frame of its own; its time goes to the next one that moves.
# Slower than a few metres a second the game's leaps also last two to four frames: one wheel
# 40-70 mm up and staying there, then back (in that drive at 3 and 11 km/h, each knocking at
# full strength; 22 knocks of half strength or more in 49 min, nearly all in yards). A rolling
# wheel cannot rise that fast: over a step of height h the wheel's centre climbs at most about
# sqrt(2h/r) per metre driven, 0.45 for a 5 cm kerb under a 0.5 m wheel. So a wheel that reads
# further from where it was than LEAP_SLOPE per metre driven plus LEAP_FLOOR per second (the
# cab rocking, a landing) is held where it was, until it comes back within reach. One that
# stays out longer than LEAP_HOLD is where the wheel is from then on, without a knock. From
# about 30 km/h the reach is wider than any leap seen, so the road's own steps are left alone.
LEAP_SLOPE = 0.5  # m of travel per m driven
LEAP_FLOOR = 0.5  # m/s
LEAP_HOLD = 0.15  # s
# Gear shifts, after the 12 Eaton Fuller shifts (Zeemods) the game plays at each
# gear change: the lever comes out of gear in a soft knock and slides across
# the gate, and about 90 ms later the next gear goes in with a clack that rings
# on in the gearbox. Most of the sound sits above 500 Hz; of what the grips play
# (30-250 Hz) the clack peaks near 140 Hz, with next to nothing under 100 Hz.
# Their median 30-250 Hz envelope, aligned at the clack, holds near -16 dB for
# the 120 ms before it, drops 7 dB within 10 ms after it and is 20 dB down
# after about 140 ms. The hits were fitted to it through a model of the grip
# motor, at the felt peak of the shift before (2.8 dB rms off; that one, a 62 Hz
# hit over a 38 Hz body, 8.8). They keep the recordings' timeline from the gear
# change, where the clack comes about 140 ms in, so it lands with the sound.
SHIFT_HZ = 140.0
# (s after the gear change, strength at gear gain 1, decay s); the clack's edge and its ring start together
SHIFT_HITS = ((0.02, 0.19, 0.16),  # the lever out of gear and across
              (0.14, 0.95, 0.005),  # the clack
              (0.14, 0.43, 0.08))  # its ring
SHIFT_COOLDOWN = 0.15  # s
# A torque converter's gearbox (an Allison) shifts under power instead: one clutch hands
# the drive to the next with no lever and no break in the pull, and the converter cushions
# it. What reaches the cab is a soft push as the next gear takes hold, firmer the harder the
# engine pulls, next to nothing coasting. No recording of one exists here: the stock game
# gives the Allison no shift sound, and the only Allison clips (a traffic bus's) are its
# whine. So this is reasoned, not measured: a rounded swell at the cab sway's pitch, at its
# height 60 ms in and 20 dB down by 0.4 s, and the bed does not duck under it, as the drive
# never lets go. Telemetry does not say which gearbox the truck has (Gains.gearbox).
AMT, CONVERTER = "amt", "converter"  # automated or manual; torque converter
GEARBOXES = (AMT, CONVERTER)
CONVERTER_SHIFT_HZ = CAB_SWAY_HZ
# strength at gear gain 1, coasting and at full throttle; the swell's height is 2/3 of it
CONVERTER_SHIFT = (0.2, 0.9)
CONVERTER_SHIFT_ATTACK, CONVERTER_SHIFT_DECAY = 0.06, 0.15  # s
# Range selector and splitter, after the Zeemods clips the game plays when one is
# flipped: the knob clicks at once (the range's near 140 Hz, the splitter's a low
# 60 Hz thock 16 ms in, both 10 dB down within 5 ms), then the gearbox's air shift
# clacks near 170 Hz (the clips' centroid within 30-250 Hz). The range shift
# swells to its loudest about 170 ms in, as strong as a gear shift (the clips have
# it 3 dB louder, which would only drive it into the output limit); the splitter's
# two clacks 120 and 180 ms in are 12 dB softer. Fitted through the grip motor
# model: range 5.0 dB rms off the clips' median envelope, splitter 8.6 (its two
# clips disagree on the timing). The knobs keep the old tap's felt peak.
# per selector, range then splitter: (s after the flip, Hz, strength at gain 1, decay s)
SHIFTER_HITS = (((0.0, 140.0, 1.0, 0.005),  # the knob
                 (0.13, 170.0, 0.38, 0.005), (0.13, 170.0, 0.23, 0.08),  # a first clack and its ring
                 (0.17, 170.0, 0.63, 0.005), (0.17, 170.0, 0.38, 0.08)),  # the shift going home
                ((0.016, 60.0, 1.0, 0.003),
                 (0.12, 170.0, 0.133, 0.005), (0.12, 170.0, 0.08, 0.06),
                 (0.18, 170.0, 0.133, 0.005), (0.18, 170.0, 0.08, 0.06)))


@functools.lru_cache(maxsize=256)  # the game names a few dozen substances
def surface_of(name: str) -> Surface | None:
    """The texture of a substance; None for no ground at all."""
    name = name.lower()
    if not name:
        return None
    for key, surface in SURFACES:
        if key in name:
            return surface
    return DEFAULT_SURFACE


_BLENDED = ("pitch", "stones", "crunch", "q", "swell", "swell_hz", "seam_share")


def side_surface(wheels: list[Wheel]) -> Surface:
    """One side's mix of surfaces: mean strength, the rest weighted by strength. Seams are not
    averaged: a wheel on planks beside one on asphalt drums at the planks' spacing, softer."""
    return _side_surface(tuple(w.substance for w in wheels))


@functools.lru_cache(maxsize=256)  # each side's substances, which change a few times a minute
def _side_surface(substances: tuple[str, ...]) -> Surface:
    found = [s for s in map(surface_of, substances) if s is not None]
    total = sum(s.level for s in found)
    if not total:
        return Surface(0.0, DEFAULT_SURFACE.pitch)
    seamed = max(found, key=lambda s: s.level * s.seam_share)
    return Surface(total / len(substances), seams=seamed.seams, seam_hz=seamed.seam_hz, seam_q=seamed.seam_q,
                   **{name: sum(s.level * getattr(s, name) for s in found) / total for name in _BLENDED})


TOGGLEABLE = ("engine", "road", "bumps", "rumble", "wheelspin", "gear", "retarder", "impact", "trigger",
              "pedal", "squeal", "trailer", "park_brake", "air_brake", "blinker", "engine_start",
              "shifter", "game_events")

# Event bursts duck the continuous effects on their channel so they stand out
# over the driving bed (sidechain ducking). Bumps come from the road itself
# and fire constantly on rough surfaces, and the brakes vent on every release
# of the pedal, so neither ducks it.
DUCK_EXEMPT = ("bumps", "air_brake")
# Letting go of the service brake vents the brake chambers, as recorded: a hiss
# that swells in tens of milliseconds and falls 20 dB within 0.17-0.31 s (all
# four release sets of the Zeemods Western Star and Freightliner packs agree),
# louder the harder it braked. Only one set (the 4900's) opens with a valve
# thump at 50-90 Hz; the others, the stock Cascadia among them, have next to
# nothing below 100 Hz, so the thump is a soft one.
AIR_BRAKE_THUMP = 0.25  # at gain 1, of the hiss's 0.5
AIR_BRAKE_HISS_DECAY = 0.105  # s: 20 dB down 0.24 s after it peaks
# A pedal hitting the floor knocks once on the trigger. At 0.8 it reached 90-112 of 112
# there, four or five times the trigger's own bed, and was felt as a jolt.
PEDAL_FLOOR = 0.4
AIR_BRAKE_HOLD = 0.3  # s the pedal must be held; a dab sends too little air to hear
AIR_BRAKE_ON, AIR_BRAKE_OFF = 0.1, 0.03  # pedal travel that holds it, and that lets go
# The flasher relay, after the relay clicks of Sound Fixes Pack and Zeemods: within
# 30-250 Hz it sits at the top (centroid 165-200 Hz) and falls 10 dB within 2 ms.
# One pack clicks as hard when the lamp goes off, the other 20 dB softer.
BLINKER_HZ = 190.0
BLINKER_DECAY = 0.003  # s
# Coupling a trailer, after Sound Fixes Pack's 11 coupling clips: not one thump but
# the fifth wheel clattering for about 0.6 s, a knock every 20-40 ms (median 30),
# mostly 7 dB under the loudest, which lands anywhere up to 0.45 s in (median 0.29).
# The knocks centre on 85-150 Hz (median 118) and fall 10 dB within about 5 ms.
COUPLE_S = 0.6
COUPLE_GAP = (0.02, 0.04)  # s between knocks
COUPLE_MAIN = (0.15, 0.45)  # s, where the loudest knock lands
COUPLE_HZ = (85.0, 150.0)
COUPLE_LOUDEST = 1.22  # at gain 1: the felt peak of the single 40 Hz thump it replaces
COUPLE_KNOCKS = (0.25, 0.7)  # the others, against the loudest
COUPLE_DECAY = 0.004  # s
# Starting and stopping, after the start and stop clips of two engine packs
# (Zeemods DD60 and Cummins X15). The starter engages with a knock and cranks for
# about a second, a compression knock every 0.12 s or so (X15's exhaust); then the
# engine catches, firing faster and faster (DD60's exhaust: 53 ms between the first
# firings, 27 ms by 0.3 s), loudest 0.2-0.3 s after catching and 15 dB down by
# 0.5 s, where the engine effect takes over. Stopping, it fires on at idle for about
# 0.3 s, then falls 12 dB and runs down over most of a second, slower and sparser.
# Like the effect before, this takes the game to report the engine on as the
# starter begins.
ENGINE_FIRING_HZ = 80.0  # the exhaust pulses' centroid within 30-250 Hz (DD60 83-94, X15 72-85)
ENGINE_FIRING_DECAY = 0.012  # s
ENGINE_START_PEAK = 1.18  # the catch's loudest firing at gain 1: the felt peak of the start before
ENGINE_CRANK = ((0.311, -14), (0.476, -10), (0.588, -14), (0.71, -14), (0.827, -12), (0.947, -16))  # (s, dB)
ENGINE_CATCH_AT = 1.04  # s
ENGINE_CATCH = ((0.0, -15), (0.053, -10), (0.095, -10), (0.138, -6), (0.173, -8), (0.205, 0), (0.235, -4),
                (0.262, -5), (0.29, 0), (0.317, 0), (0.343, -4), (0.392, -7), (0.418, -9), (0.451, -8),
                (0.488, -12), (0.535, -17))  # (s after catching, dB)
ENGINE_STOP_PEAK = 0.73  # the stop's loudest firing at gain 1: the felt peak of the stop before
ENGINE_STOP = ((0.003, -4), (0.033, -1), (0.068, -2), (0.103, -1), (0.138, -4), (0.173, 0), (0.209, -5),
               (0.243, -1), (0.279, -2), (0.318, -4), (0.357, -12), (0.384, -13), (0.42, -12), (0.486, -14),
               (0.515, -16), (0.627, -16), (0.652, -15), (0.677, -17))  # (s, dB)
DUCK_FULL = 0.25  # burst envelope that ducks by the full Gains.ducking
DUCK_ATTACK = 1.0 - math.exp(-1.0 / (0.001 * SAMPLE_RATE))  # 1 ms, avoids a step in the bed
DUCK_RELEASE = math.exp(-1.0 / (0.08 * SAMPLE_RATE))  # 80 ms back to full
DUCK_LOOKAHEAD = 0.02  # delayed bursts start ducking this early, leaving a gap

GRIP, TRIGGER, BOTH = "grip", "trigger", "both"
ROUTES = (GRIP, TRIGGER, BOTH)
DEFAULT_ROUTES = {name: TRIGGER if name in ("trigger", "pedal", "squeal") else GRIP for name in TOGGLEABLE}
# The trigger channel is one waveform for whichever triggers it plays on (Block.side).
# The mix keeps what belongs to one pedal apart from the rest until the block is made:
# the brake's alone plays on the left trigger, the throttle's alone on the right,
# anything else on the channel, or both pedals at once, on both (_trigger_side).
BOTH_TRIGGERS, BRAKE_TRIGGER, THROTTLE_TRIGGER = 2, 3, 4  # mix slots after the two grips
TRIGGER_SLOTS = (BOTH_TRIGGERS, BRAKE_TRIGGER, THROTTLE_TRIGGER)


@dataclass
class Gains:
    master: float = 1.0
    grip_level: float = 1.0  # final output strength per channel, after all effects
    trigger_level: float = 1.0
    # The player sits in the cab holding the wheel, often for hours. The engine is a hum under
    # the road, and the cab's clicks stand out.
    engine: float = 0.1
    road: float = 0.5
    bumps: float = 1.0
    rumble: float = 0.8
    wheelspin: float = 0.7
    # Gear shift, throttle/brake and its pulse are the developer's own after a 220 mile haul
    # (2026-10-28): the shift clacks near 90 of 112 even at 0.6, and anything stronger tires
    # the hands.
    gear: float = 0.6
    retarder: float = 0.3
    impact: float = 1.0
    trigger: float = 0.2  # held nearly all the time
    # how deep the throttle trigger pulses at the engine firing rate; 0 keeps
    # it steady, 0.8 is also the fixed pulse of earlier versions
    throttle_pulse: float = 0.8
    # how far engine, road and throttle/brake fade while what drives them holds steady; 0 keeps
    # them constant. On the developer's 220 mile haul 0.6 takes the road 6.6 dB down overall
    # (under 60% of itself for 86% of the drive) and the trigger 2.5 dB; 0.4 takes them 3.9 and 1.6.
    steady_fade: float = 0.6
    cylinders: int = 6
    # how a gear change feels: AMT (the lever's knock and the clack) or CONVERTER (a soft push)
    gearbox: str = AMT
    # m/s of sudden suspension travel; 0.2 brings a 60 fps game's short joints, which a
    # frame can straddle, close to what a continuous clock sees at 0.25
    bump_threshold: float = 0.2
    impact_threshold: float = 12.0  # m/s^2 change from one game frame to the next
    # event effects
    pedal: float = 0.6
    squeal: float = 0.4
    trailer: float = 0.9
    park_brake: float = 0.7
    engine_start: float = 0.8
    shifter: float = 0.7
    # What the driver only hears, never feels: the flasher relay, the brakes venting on every
    # release, the game's alerts. Softer than the cab's knocks; with the road fading on steady
    # stretches they still come through.
    air_brake: float = 0.3
    blinker: float = 0.5
    game_events: float = 0.5
    # how far the continuous effects dip while an event plays; 0 never
    ducking: float = 0.7
    # effects switched off; their gains are kept so switching back restores them
    disabled: list[str] = field(default_factory=list)
    # effect -> GRIP, TRIGGER or BOTH; missing or unknown entries use DEFAULT_ROUTES
    routes: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTES))


FLAT = (1.0, 1.0)
# Every block looks up the eq of each effect sounding, once per burst too: a sixth of the
# synth's time on the scripted drive. Bursts keep their frequency, so most lookups repeat;
# the continuous effects' follow the revs and the speed and are cleared out now and then.
PAIR_CACHE = 512


class ResonanceCut:
    """A motor's resonance taken back out of its drive: a peaking cut (RBJ biquad), 1 away from it.

    A pulse near the resonance ends before it has built the resonance up, then
    leaves it ringing for tens of milliseconds: through a motor modelled on the
    default grip curve (+10 dB at 65 Hz, Q 4.4) a 65 Hz joint knock reached a third
    of its strength and lasted 2.4 times as long, soft and round, and every knock
    blurred into the same hum. Cut by the curve's
    own peak, the drive cancels that ringing and the hand gets the pulse as
    shaped. Each effect's eq makes the cut up at its own frequency (Equalizer.pair),
    so a steady tone feels as before.
    """

    def __init__(self, center: float, depth_db: float, q: float) -> None:
        self.center, self.depth_db, self.q = center, depth_db, q
        a = 10.0 ** (-depth_db / 40.0)
        w = TAU * center / SAMPLE_RATE
        alpha = math.sin(w) / (2.0 * q)
        a0 = 1.0 + alpha / a
        self.b0, self.b1, self.b2 = (1.0 + alpha * a) / a0, -2.0 * math.cos(w) / a0, (1.0 - alpha * a) / a0
        self.a1, self.a2 = -2.0 * math.cos(w) / a0, (1.0 - alpha / a) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def magnitude(self, freq: float) -> float:
        z = cmath.exp(-1j * TAU * freq / SAMPLE_RATE)
        return abs((self.b0 + self.b1 * z + self.b2 * z * z) / (1.0 + self.a1 * z + self.a2 * z * z))

    def next(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        if not x and abs(y) < 1e-6 and abs(self.y1) < 1e-6:
            y = 0.0  # rung out: back to exact silence, which mutes the trigger route
        self.x2, self.x1, self.y2, self.y1 = self.x1, x, self.y1, y
        return y


class Equalizer:
    """How much more drive each frequency needs to feel as strong as the reference, per output.

    The voice coils resonate (grips near 65 Hz, triggers near 100 Hz), so the
    same drive feels far weaker off resonance. The motor calibration measures it
    by matching tones against the reference by feel; until then a motor gets
    DEFAULT. Between the measured points the gain follows a straight line in dB
    over log frequency, and past the ends it holds. A curve plays at its
    channel's LEVEL, so it shifts the balance between frequencies without
    making the channel louder overall.

    A scale per effect only fits a steady tone. The resonance a curve shows
    also rings after every short pulse, so each channel's output goes through
    a ResonanceCut fitted to the curve, and pair() makes it up at each effect's
    frequency.
    """

    LIMITS = (0.25, 4.0)
    CHANNELS = ("grip", "trigger")
    RESONANCE_MIN_DB = 3.0  # a shallower dip is left to the per-effect gains alone
    RESONANCE_Q = (1.0, 8.0)  # range fitted; past 8 a slightly misplaced cut would miss the peak
    # A grip curve lifts most of the band 3-4x over the 65 Hz reference. At full level that
    # doubled the grips on the scripted drive and put 3% of their output into the soft limit
    # (0.2% flat); at 0.4 their loudness and limiting match the flat curve's again. The trigger
    # effects sit at the triggers' own 100 Hz reference and need no scaling.
    LEVEL = {"grip": 0.4, "trigger": 1.0}
    # Matched by feel on the developer's APEX in the window's calibration (2026-10-25), then
    # smoothed in dB (1-2-1 along each side; the reference's neighbours kept as measured), which
    # moves no point by more than the 1.5 dB staircase step.
    DEFAULT = {"grip": {"30": 3.89, "40": 3.55, "50": 2.94, "65": 1.0, "80": 2.27, "100": 2.98, "130": 3.47,
                        "160": 3.3, "200": 3.16, "250": 3.27},
               "trigger": {"50": 4.0, "65": 3.63, "80": 2.7, "100": 1.0, "130": 2.27, "160": 2.1, "200": 2.15,
                           "250": 2.12}}

    @classmethod
    def for_player(cls, own: dict) -> Equalizer:
        """The player's own calibration, and DEFAULT for a motor they have not measured."""
        return cls({**cls.DEFAULT, **own})

    def __init__(self, curves: dict | None = None) -> None:
        self.curves = curves or {}  # as given, for the debug log to keep
        self.points: dict[str, list[tuple[float, float]]] = {}
        for channel in self.CHANNELS:
            stored = (curves or {}).get(channel)
            level = math.log(self.LEVEL[channel])
            points = []
            for freq, gain in (stored.items() if isinstance(stored, dict) else ()):
                try:
                    freq, gain = float(freq), float(gain)
                except (TypeError, ValueError):
                    continue
                if freq > 0 and gain > 0:
                    points.append((math.log(freq), math.log(min(max(gain, self.LIMITS[0]), self.LIMITS[1])) + level))
            self.points[channel] = sorted(points)
        self.flat = not any(self.points.values())
        self.resonances = {channel: self._resonance(self.points[channel]) for channel in self.CHANNELS}
        # stateless copies for their magnitudes
        self.shapes = {channel: ResonanceCut(*found) if found else None for channel, found in self.resonances.items()}
        self._pairs: dict[float, tuple[float, float]] = {}  # pair() by frequency (PAIR_CACHE)

    def _resonance(self, points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
        """(Hz, dB, Q) of the resonance a curve shows: its lowest point against the median of the
        points from half an octave above, as wide as the points within an octave of it say."""
        if len(points) < 4:
            return None
        x0, y0 = min(points, key=lambda point: point[1])
        above = sorted(y for x, y in points if x >= x0 + math.log(2.0) / 2)
        if len(above) < 2:
            return None
        floor = (above[(len(above) - 1) // 2] + above[len(above) // 2]) / 2
        depth = (floor - y0) * 20.0 / math.log(10.0)
        if depth < self.RESONANCE_MIN_DB:
            return None
        center = math.exp(x0)
        # how far each neighbour stands out over the floor; one that needs more than the floor is outside it
        near = [(math.exp(x), max(0.0, (floor - y) * 20.0 / math.log(10.0)))
                for x, y in points if 0 < abs(x - x0) <= math.log(2.0)]
        low, high = self.RESONANCE_Q

        def misfit(q: float) -> float:
            shape = ResonanceCut(center, depth, q)
            return sum((-20.0 * math.log10(shape.magnitude(freq)) - lift) ** 2 for freq, lift in near)

        q = min((low + n * 0.1 for n in range(round((high - low) / 0.1) + 1)), key=misfit)
        return center, depth, q

    def cut(self, channel: str) -> ResonanceCut | None:
        """A fresh filter for one output of this channel; None when its curve shows no resonance."""
        found = self.resonances[channel]
        return ResonanceCut(*found) if found else None

    def gain(self, channel: str, freq: float) -> float:
        """The curve: how much a steady tone at freq is driven, all told."""
        points = self.points[channel]
        if not points or freq <= 0:
            return 1.0
        x = math.log(freq)
        if x <= points[0][0]:
            return math.exp(points[0][1])
        if x >= points[-1][0]:
            return math.exp(points[-1][1])
        i = bisect.bisect(points, (x, math.inf))
        (x0, y0), (x1, y1) = points[i - 1], points[i]
        return math.exp(y0 + (y1 - y0) * (x - x0) / (x1 - x0))

    def pair(self, freq: float) -> tuple[float, float]:
        """(grip, trigger) gain for a vibration at freq, ahead of its channel's ResonanceCut."""
        if self.flat:
            return FLAT
        found = self._pairs.get(freq)
        if found is None:
            if len(self._pairs) >= PAIR_CACHE:
                self._pairs.clear()
            found = self._pairs[freq] = tuple(self.gain(channel, freq) / (shape.magnitude(freq) if shape else 1.0)
                                              for channel, shape in self.shapes.items())
        return found


def route_of(gains: Gains, name: str) -> str:
    route = gains.routes.get(name)
    return route if route in ROUTES else DEFAULT_ROUTES[name]


def effective_gains(gains: Gains) -> Gains:
    """The gains the synth should use: disabled effects at zero."""
    result = replace(gains, disabled=list(gains.disabled), routes=dict(gains.routes))
    for name in gains.disabled:
        if name in TOGGLEABLE:
            setattr(result, name, 0.0)
    return result


GAME_EVENT_LABELS = {"delivered": N_("배송 완료"), "fined": N_("벌금"), "tollgate": N_("톨게이트"),
                     "ferry": N_("페리"), "train": N_("기차"), "refuel": N_("주유")}


class Oscillator:
    def __init__(self) -> None:
        self.phase = 0.0

    def next(self, freq: float) -> float:
        value = math.sin(TAU * self.phase)
        self.phase = (self.phase + freq / SAMPLE_RATE) % 1.0
        return value


def jake_pulse(phase: float) -> float:
    """One firing cycle of the engine brake's knock: peak 1 at JAKE_PEAK, zero mean."""
    u = phase / JAKE_PEAK
    return (u * math.exp(1.0 - u) - _JAKE_MEAN) / (1.0 - _JAKE_MEAN)


class Resonator:
    """2-pole band-pass (RBJ biquad, 0 dB at the centre); it can be retuned between blocks."""

    def __init__(self, center: float, q: float) -> None:
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0
        self.center = self.q = 0.0
        self.tune(center, q)

    def tune(self, center: float, q: float) -> None:
        if (center, q) == (self.center, self.q):
            return
        self.center, self.q = center, q
        w = TAU * center / SAMPLE_RATE
        alpha = math.sin(w) / (2 * q)
        a0 = 1 + alpha
        self.b0, self.b2 = alpha / a0, -alpha / a0
        self.a1, self.a2 = -2 * math.cos(w) / a0, (1 - alpha) / a0

    def next(self, x: float) -> float:
        y = self.b0 * x + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1, self.y2, self.y1 = self.x1, x, self.y1, y
        return y

    def impulse_peak(self) -> float:
        """Largest swing this tuning rings with after a unit impulse."""
        x1 = x2 = y1 = y2 = 0.0
        peak = 0.0
        for n in range(int(SAMPLE_RATE / self.center) + 2):  # the first crest comes within a cycle
            x = 1.0 if n == 0 else 0.0
            y = self.b0 * x + self.b2 * x2 - self.a1 * y1 - self.a2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            peak = max(peak, abs(y))
        return peak


class BandNoise:
    """White noise through a band-pass."""

    def __init__(self, center: float, q: float, seed: int) -> None:
        self.filter = Resonator(center, q)
        self.rng = random.Random(seed)

    def next(self) -> float:
        return self.filter.next(self.rng.uniform(-1.0, 1.0)) * 2.5  # about 0.6 RMS, peaks near 2.3


class Grains:
    """Random clicks, each ringing the band-pass with a peak up to 1: mostly small, now and then a big one."""

    Q = 1.5  # rings about 7 ms, so clicks a few metres apart stay single

    def __init__(self, seed: int, center: float = 100.0) -> None:
        self.filter = Resonator(center, self.Q)
        self.rng = random.Random(seed)
        self.rate = 0.0  # clicks per second
        self.gain = 1.0 / self.filter.impulse_peak()

    def tune(self, center: float, rate: float) -> None:
        if center != self.filter.center:
            self.filter.tune(center, self.Q)
            self.gain = 1.0 / self.filter.impulse_peak()
        self.rate = rate

    def next(self) -> float:
        click = 0.0
        if self.rate and self.rng.random() < self.rate / SAMPLE_RATE:
            click = (0.2 + 0.8 * self.rng.random() ** 3) * self.gain * self.rng.choice((-1.0, 1.0))
        return self.filter.next(click)


class Seams:
    """Seams a set distance apart under a rolling tyre, each a knock ringing the band-pass: slab
    joints a few times a second, or planks and grating bars so close they drum or sing at
    speed / spacing. At SEAM_SPEED they come out at unit RMS."""

    def __init__(self, seed: int) -> None:
        self.filter = Resonator(90.0, 1.5)
        self.rng = random.Random(seed)
        self.spacing = self.step = self.travel = 0.0
        self.next_at = 1.0
        self.gain = 0.0

    def tune(self, spacing: float, center: float, q: float, speed: float) -> None:
        if (center, q) != (self.filter.center, self.filter.q) or spacing != self.spacing:
            self.filter.tune(center, q)
            self.gain = 1.0 / math.sqrt(self._power(spacing)) if spacing else 0.0
            if spacing != self.spacing:
                self.travel, self.next_at = 0.0, spacing
        self.spacing = spacing
        self.step = speed / SAMPLE_RATE

    def _power(self, spacing: float) -> float:
        """Mean square of unit knocks spacing apart at SEAM_SPEED. Joints ring out between
        knocks, but grating bars come at the bars' own ringing and pile up, so it is played
        through rather than worked out from one knock's ring."""
        ring = Resonator(self.filter.center, self.filter.q)
        period = spacing * SAMPLE_RATE / SEAM_SPEED
        total, count, until = 0.0, 0, period
        settle, end = SAMPLE_RATE // 2, SAMPLE_RATE // 2 + max(SAMPLE_RATE, round(4 * period))
        for n in range(end):
            knock = 0.0
            if n >= until:
                until += period
                knock = 1.0
            y = ring.next(knock)
            if n >= settle:
                total += y * y
                count += 1
        return total / count

    def next(self) -> float:
        knock = 0.0
        if self.spacing:
            self.travel += self.step
            if self.travel >= self.next_at:
                self.travel -= self.next_at
                self.next_at = self.spacing * (1.0 + SEAM_JITTER * self.rng.uniform(-1.0, 1.0))
                knock = self.gain * (0.85 + 0.3 * self.rng.random())
        return self.filter.next(knock)


class Swell:
    """Slow random gain around 1: white noise through two one-pole low-passes."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.y1 = self.y2 = 0.0
        self.depth = self.hz = 0.0
        self.tune(0.25, 4.0)

    def tune(self, depth: float, hz: float) -> None:
        """depth: standard deviation around 1; hz: corner of the low-passes."""
        if (depth, hz) == (self.depth, self.hz):
            return
        self.depth, self.hz = depth, hz
        self.a = a = math.exp(-TAU * hz / SAMPLE_RATE)
        # deviation of uniform noise (variance 1/3) after the two poles
        self.scale = depth / math.sqrt((1 - a) ** 4 * (1 + a * a) / (1 - a * a) ** 3 / 3)
        self.rms = math.sqrt(1.0 + depth ** 2)  # what the swell adds to the strength, for the texture to take back out

    def next(self) -> float:
        self.y1 = self.a * self.y1 + (1 - self.a) * self.rng.uniform(-1.0, 1.0)
        self.y2 = self.a * self.y2 + (1 - self.a) * self.y1
        return max(0.0, 1.0 + self.scale * self.y2)


class Texture:
    """One side's road texture: noise around the surface's pitch, stones clicking, seams at a
    steady beat, the cab rattling on rough ground, all swelling and fading as the surface does."""

    STONE_GAIN = 5.0  # few and mostly small; a big one knocks through the noise at full strength
    RATTLE_GAIN = 3.0  # at full roughness about a third more strength, in knocks
    NOISE_RMS = 0.6  # of the hiss at its reference band (BandNoise at 80 Hz, Q 1.2); the seams match it

    def __init__(self, seed: int) -> None:
        self.noise = BandNoise(DEFAULT_SURFACE.pitch, 1.2, seed)
        self.stones = Grains(seed + 100)
        self.rattle = Grains(seed + 200, RATTLE_HZ)
        self.seams = Seams(seed + 400)
        self.swell = Swell(seed + 300)
        self.crunch = 0.0
        self.noise_gain = 1.0
        self.hiss = 1.0  # share of the hiss's strength left beside the seams
        self.seam_gain = 0.0
        self.pitch = DEFAULT_SURFACE.pitch

    def tune(self, surface: Surface, speed: float, roughness: float, cab_shake: float = 0.0) -> None:
        pace = min(1.0, speed / 25.0)
        self.pitch = pitch = surface.pitch * (PITCH_STANDING + (1.0 - PITCH_STANDING) * pace)
        self.noise.filter.tune(pitch, surface.q)
        # A band-pass lets through noise in proportion to its width, centre over Q:
        # keep the strength of the 80 Hz, Q 1.2 band every surface used to share.
        self.noise_gain = math.sqrt(80.0 / pitch * surface.q / 1.2)
        self.stones.tune(pitch * 1.25, surface.stones * speed)
        self.crunch = surface.crunch
        # seams and hiss are independent: their powers add up to the hiss's alone
        self.hiss = math.sqrt(1.0 - surface.seam_share)
        self.seam_gain = self.NOISE_RMS * math.sqrt(surface.seam_share)
        self.seams.tune(surface.seams, surface.seam_hz, surface.seam_q, speed)
        self.swell.tune(surface.swell, surface.swell_hz)
        low, full = RATTLE_ROUGH
        cab_low, cab_full = CAB_RATTLE
        self.rattle.rate = RATTLE_RATE * min(1.0, max(0.0, (roughness - low) / (full - low),
                                                 (cab_shake - cab_low) / (cab_full - cab_low)))

    def next(self) -> float:
        value = (1.0 - self.crunch) * self.hiss * self.noise_gain * self.noise.next()
        if self.crunch:
            value += self.crunch * self.STONE_GAIN * self.stones.next()
        if self.seam_gain:
            value += self.seam_gain * self.seams.next()
        value += self.RATTLE_GAIN * self.rattle.next()  # also while the last knock rings out
        return value * self.swell.next() / self.swell.rms


class Burst:
    """Decaying sine pulse for discrete events, optionally delayed.

    phase 0.25 starts at the crest instead of zero: a hard edge, not a swell.
    attack_s ramps the envelope in, for sounds that swell, like venting air.
    ducks=False leaves the continuous effects alone while it plays.
    trigger_slot puts it on one pedal's trigger when it plays on the trigger.
    """

    def __init__(self, freq: float, amplitude: float, decay_s: float, delay_s: float = 0.0,
                 phase: float = 0.0, attack_s: float = 0.0, ducks: bool = True,
                 trigger_slot: int = BOTH_TRIGGERS) -> None:
        self.freq, self.amplitude, self.decay_s, self.attack_s = freq, amplitude, decay_s, attack_s
        self.ducks, self.trigger_slot = ducks, trigger_slot
        self.age = -delay_s
        self.phase = phase

    def _envelope(self) -> float:
        envelope = self.amplitude * math.exp(-self.age / self.decay_s)
        return envelope * self.age / self.attack_s if self.age < self.attack_s else envelope

    @property
    def level(self) -> float:
        """Current envelope, before next() advances it; full just ahead of a delayed start."""
        if self.age >= 0:
            return self._envelope()
        return self.amplitude if self.age >= -DUCK_LOOKAHEAD else 0.0

    def _carrier(self) -> float:
        value = math.sin(TAU * self.phase)
        self.phase = (self.phase + self.freq / SAMPLE_RATE) % 1.0
        return value

    def retrigger(self, amplitude: float, freq: float | None = None, decay_s: float | None = None) -> None:
        """Restart the envelope at a new peak, optionally retuned; the carrier keeps running."""
        self.amplitude, self.age = amplitude, 0.0
        self.freq = freq or self.freq
        self.decay_s = decay_s or self.decay_s

    def next(self) -> float:
        if self.age < 0:
            self.age += 1.0 / SAMPLE_RATE
            return 0.0
        value = self._carrier() * self._envelope()
        self.age += 1.0 / SAMPLE_RATE
        return value

    @property
    def done(self) -> bool:
        return self.age > self.attack_s + self.decay_s * 5


class Squeal:
    """Brake squeal as recorded: separate squeals of a few tenths of a second that swell in
    and stop short, with gaps between and each a little off the last in pitch."""

    HZ = 230.0
    LENGTH = (0.15, 0.5)  # s
    GAP = (0.03, 0.25)  # s
    ATTACK, RELEASE = 0.08, 0.04  # s

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.osc = Oscillator()
        self.age = self.length = self.gap = 0.0
        self.pitch = 1.0

    def restart(self) -> None:
        """The next sample starts a squeal, rather than wherever the last one left off."""
        self.age = self.length + self.gap

    def next(self) -> float:
        if self.age >= self.length + self.gap:
            self.age = 0.0
            self.length, self.gap = self.rng.uniform(*self.LENGTH), self.rng.uniform(*self.GAP)
            self.pitch = self.rng.uniform(0.92, 1.08)
        age = self.age
        self.age += 1.0 / SAMPLE_RATE
        envelope = min(1.0, age / self.ATTACK, (self.length - age) / self.RELEASE) if age < self.length else 0.0
        return envelope * self.osc.next(self.HZ * self.pitch)


class NoiseBurst(Burst):
    """Decaying band-limited noise: air hiss, cranking."""

    def __init__(self, center: float, amplitude: float, decay_s: float, delay_s: float = 0.0,
                 attack_s: float = 0.0) -> None:
        super().__init__(center, amplitude, decay_s, delay_s, attack_s=attack_s)
        self.noise = BandNoise(center, 0.8, seed=random.randrange(1 << 30))

    def _carrier(self) -> float:
        return self.noise.next()


@dataclass
class Levels:
    """Last block's effect levels, for the monitor line."""

    engine: float = 0.0
    road_left: float = 0.0
    road_right: float = 0.0
    trigger: float = 0.0
    trigger_mode: str = "-"
    surface: str = ""
    max_accel: float = 0.0
    events: list[str] = field(default_factory=list)


class Synth:
    def __init__(self, gains: Gains, eq: Equalizer | None = None) -> None:
        self.g = gains
        self.eq = eq or Equalizer()
        self.cuts = (self.eq.cut("grip"), self.eq.cut("grip"), self.eq.cut("trigger"))  # left, right, trigger
        self.engine_osc = Oscillator()
        # each cylinder's own share of the idle's unevenness, the same every cycle
        self.cylinder_offsets = [random.Random(11 + n).uniform(-1.0, 1.0) for n in range(12)]
        self.cylinder = 0
        self.firing_gain = 1.0
        self.trigger_osc = Oscillator()
        self.retarder_osc = Oscillator()
        self.squeal = Squeal(seed=4)
        self.texture = (Texture(seed=1), Texture(seed=2))
        self.rumble_osc = (Oscillator(), Oscillator())
        self.rumble_half = (Oscillator(), Oscillator())  # locked to rumble_osc at half its rate
        self.spin_noise = BandNoise(45.0, 1.5, seed=3)
        self.spin_chatter = Oscillator()
        # (effect, side, burst); side "L"/"R" is one grip, "C" both. The trigger
        # takes every burst of an effect routed there, in the burst's trigger_slot.
        self.bursts: list[tuple[str, str, Burst]] = []
        self.trigger_slot = BOTH_TRIGGERS  # the pedal the throttle/brake effect last followed
        self.trigger_side = Route.BOTH  # where the trigger channel last played
        # previous-block state
        self.prev_amp = {"engine": 0.0, "road_l": 0.0, "road_r": 0.0, "rumble_l": 0.0, "rumble_r": 0.0,
                         "spin": 0.0, "retarder": 0.0, "jake": 0.0, "trigger": 0.0, "squeal": 0.0,
                         "harsh": 0.0, "sway_l": 0.0, "sway_r": 0.0}
        self.prev_state: dict[str, object] = {}
        self.blinker_idle = 0.0
        self.brake_held = self.brake_peak = 0.0  # how long and how hard the service brake has been held
        self.was_live = False
        self.prev_render_time: int | None = None
        self.prev_deflection: list[float] = []
        self.deflection_rate_avg: list[float] = []
        self.held_frame: tuple[Frame, list[float], float] | None = None  # the frame read next, its deflections, dt
        self.repeat_dt = 0.0  # s of repeated frames since the held one
        self.leap_held: list[float] = []  # s each wheel has been held at its last reading (LEAP_HOLD)
        self.bump_bursts: list[tuple[str, Burst] | None] = []  # each wheel's pulse and its side while it rings
        self.bump_rattles: list[Burst] = []  # the cab's knocks after big hits, all wheels together
        # truck, trailer: m/s of sudden suspension travel on average (BUMP_RIDE_*), and over how many wheels
        self.ride = [0.0, 0.0]
        self.ride_wheels = [0, 0]
        self.rng = random.Random(7)
        self.roughness = [0.0, 0.0]
        self.rough_avg = [0.0, 0.0]  # roughness over the last DETAIL_TAU, each side
        # s left of each side's rumble after its wheels left the strip; truck, then trailer
        self.rumble_hold = ([0.0, 0.0], [0.0, 0.0])
        self.spin = 0.0
        self.yaw_sign = 1.0  # +1: the yaw rate is positive turning left, as the SDK's axes say
        self.yaw_evidence = 0.0
        self.prev_gear: int | None = None  # None until the first live frame sets the reference
        self.gear_cooldown = 0.0
        self.prev_accel: tuple[float, float, float] | None = None
        self.impact_cooldown = 0.0
        self.cab_shake = 0.0  # (rad/s^2)^2 the cab has been shaking
        self.cab_sway = (0.0, 0.0)  # left, right: the sway's strength the last frame asked for
        self.sway = [0.0, 0.0]  # and where it has got to, smoothed over CAB_SWAY_TAU
        self.sway_osc = Oscillator()
        self.lock_phase = 0.0  # of the ABS-like chop, per sample
        self.was_locked = False
        self.steady_ref: dict[str, float] = {}
        self.fresh = dict.fromkeys(STEADY_BEDS, 1.0)  # 1 just after a bed's input moved, falling while it holds
        self.duck = [0.0, 0.0, 0.0]  # left grip, right grip, trigger; 0 = no ducking
        self.levels = Levels()

    # -- event detection, once per block ------------------------------------

    def _rebaseline(self) -> None:
        """Forget the edge detectors' last values as telemetry comes back.

        A gap (pause, menu, loading, a ferry, a new session or reconnect) can
        end with the truck elsewhere; comparing against the old values would
        fire bumps on every wheel, an impact or a gear change that never
        happened. The first live frame only becomes the new reference. Game
        events are one-shot flags, not states, so they still fire.
        """
        self.prev_render_time = None
        self.brake_held = self.brake_peak = 0.0
        self.prev_deflection = []
        self.held_frame = None
        self.repeat_dt = 0.0
        self.leap_held = []
        self.prev_accel = None
        self.cab_shake, self.cab_sway = 0.0, (0.0, 0.0)
        self.prev_gear = None
        self.prev_state = {key: value for key, value in self.prev_state.items() if key == "game_events"}
        # coming back is a change too: the beds start in full and settle again
        self.steady_ref = {}
        self.fresh = dict.fromkeys(STEADY_BEDS, 1.0)

    def _frame_dt(self, frame: Frame) -> float | None:
        """Game time in s since the last new frame; None when this frame was read before.

        0 when there is no usable previous frame (the first one, a hitch
        longer than MAX_FRAME_GAP, the game's timer restarting): the rate
        detectors then only take a new reference.
        """
        previous, self.prev_render_time = self.prev_render_time, frame.render_time
        if previous == frame.render_time:
            return None
        dt = 0.0 if previous is None else (frame.render_time - previous) / 1e6
        return dt if 0.0 < dt <= MAX_FRAME_GAP else 0.0

    def _wheel_events(self, frame: Frame, dt: float) -> None:
        """Bumps and roughness of the frame before this one, once this one shows its spikes (SPIKE_MIN)."""
        deflections = [w.deflection for w in frame.wheels + frame.trailer_wheels]
        if dt and self.held_frame is not None and deflections == self.held_frame[1]:
            self.repeat_dt += dt  # the game repeating a state (see SPIKE_BACK): its time belongs to the next real frame
            return
        if dt:
            dt += self.repeat_dt
            dt = dt if dt <= MAX_FRAME_GAP else 0.0  # repeated that long, it was a hitch
        self.repeat_dt = 0.0
        held, self.held_frame = self.held_frame, (frame, deflections, dt)
        if held is None:
            return
        frame, deflections, dt = held
        _, after, after_dt = self.held_frame
        if dt and after_dt and len(after) == len(deflections) == len(self.prev_deflection):
            for i, (before, now, then) in enumerate(zip(self.prev_deflection, deflections, after)):
                jump = now - before
                if abs(jump) >= SPIKE_MIN and abs(then - before) < SPIKE_BACK * abs(jump):
                    deflections[i] = before + (then - before) * dt / (dt + after_dt)
        self._drop_leaps(frame, deflections, dt)
        self._suspension(frame, deflections, dt)

    def _drop_leaps(self, frame: Frame, deflections: list[float], dt: float) -> None:
        """Hold a wheel where it was while it reads further off than rolling could take it (LEAP_*)."""
        if len(self.leap_held) != len(deflections):
            self.leap_held = [0.0] * len(deflections)
        if not dt or len(self.prev_deflection) != len(deflections):
            return
        reach = (LEAP_SLOPE * abs(frame.speed) + LEAP_FLOOR) * dt
        for i, (before, now) in enumerate(zip(self.prev_deflection, deflections)):
            if abs(now - before) <= reach:
                self.leap_held[i] = 0.0
            elif self.leap_held[i] + dt <= LEAP_HOLD:
                self.leap_held[i] += dt
                deflections[i] = before
            else:  # it stays there: where the wheel is from now on, not a movement
                self.leap_held[i] = 0.0
                self.prev_deflection[i] = now

    def _suspension(self, frame: Frame, deflections: list[float], dt: float) -> None:
        wheels = frame.wheels + frame.trailer_wheels
        for group, count in enumerate((len(frame.wheels), len(frame.trailer_wheels))):
            if self.ride_wheels[group] != count:  # another truck or trailer, or none: learn its ride afresh
                self.ride_wheels[group], self.ride[group] = count, 0.0
        if len(self.prev_deflection) != len(wheels):
            self.prev_deflection = deflections
            self.deflection_rate_avg = [0.0] * len(wheels)
            self.bump_bursts = [None] * len(wheels)
            return
        if not dt:  # no rate across a gap: the travel since then is not one movement
            self.prev_deflection = deflections
            return
        avg_k = 1.0 - math.exp(-dt / RATE_AVG_TAU)
        rough = [0.0, 0.0]
        counts = [0, 0]
        thresholds = [max(self.g.bump_threshold, min(BUMP_RIDE_MAX, BUMP_OVER_RIDE * ride)) for ride in self.ride]
        ride, ride_counts = [0.0, 0.0], [0, 0]
        for i, wheel in enumerate(wheels):
            rate = (deflections[i] - self.prev_deflection[i]) / dt  # m/s
            self.prev_deflection[i] = deflections[i]
            avg = self.deflection_rate_avg[i]
            avg = self.deflection_rate_avg[i] = avg + (rate - avg) * avg_k
            high = abs(rate - avg)
            side = 0 if wheel.pos_x < 0 else 1
            trailer = i >= len(frame.wheels)
            if not trailer:  # road texture is what the truck's own tyres feel
                rough[side] += high
                counts[side] += 1
            if not wheel.on_ground:
                continue
            ride[trailer] += high
            ride_counts[trailer] += 1
            threshold = thresholds[trailer]
            if high > threshold:  # a step in suspension travel: pothole, kerb, joint
                size = min(1.0, (high - self.g.bump_threshold) / 0.8)
                strength = size * self.g.bumps * (TRAILER_FEEL if trailer else 1.0)
                small_hz, big_hz = BUMP_HZ
                freq = small_hz + (big_hz - small_hz) * size + (TRAILER_BUMP_HZ if trailer else 0.0)
                decay = BUMP_DECAY[0] + (BUMP_DECAY[1] - BUMP_DECAY[0]) * size
                rattles = size >= BUMP_RATTLE_FROM and not trailer
                entry = self.bump_bursts[i]
                if entry is not None and not entry[1].done:
                    # One pulse per wheel, which keeps the work bounded by the wheel
                    # count once trailers multiply the wheels. A step trips several
                    # frames, down and back up; striking again on each one drew a
                    # 25 ms knock out into 60-150 ms of hum. Only the step's next
                    # frame may still strike it harder, then the wheel waits.
                    where, current = entry
                    if strength > current.level and (current.age < BUMP_RESTRIKE_S or current.age >= BUMP_GAP_S):
                        current.retrigger(strength, freq, decay)
                        if rattles:
                            self._rattle(where, strength, size)
                    continue
                where = "C" if abs(wheel.pos_x) < 0.15 else "LR"[side]
                burst = Burst(freq, strength, decay, phase=0.25)  # a hit lands on its edge
                self.bursts.append(("bumps", where, burst))
                self.bump_bursts[i] = (where, burst)
                if rattles:
                    self._rattle(where, strength, size)
        if abs(frame.speed) >= BUMP_RIDE_SPEED:
            ride_k = 1.0 - math.exp(-dt / BUMP_RIDE_TAU)
            for group in (0, 1):
                if ride_counts[group]:
                    self.ride[group] += (ride[group] / ride_counts[group] - self.ride[group]) * ride_k
        rough_k = 1.0 - math.exp(-dt / ROUGH_TAU)
        for side in (0, 1):
            value = rough[side] / counts[side] if counts[side] else 0.0
            self.roughness[side] += (value - self.roughness[side]) * rough_k
            self.rough_avg[side] += (self.roughness[side] - self.rough_avg[side]) * (1.0 - math.exp(-dt / DETAIL_TAU))

    def _rattle(self, where: str, strength: float, size: float) -> None:
        """A few small knocks over the next BUMP_RATTLE_S, fading: the cab settling after a hit."""
        self.bump_rattles = [knock for knock in self.bump_rattles if not knock.done]
        for _ in range(int(2 + 4 * size)):
            if len(self.bump_rattles) >= BUMP_RATTLE_MAX:
                return
            delay = self.rng.uniform(0.04, BUMP_RATTLE_S)
            amplitude = strength * 0.3 * self.rng.uniform(0.4, 1.0) * (1.0 - delay / (BUMP_RATTLE_S + 0.05))
            knock = Burst(self.rng.uniform(55.0, 85.0), amplitude, 0.008, delay_s=delay, phase=0.25)
            self.bursts.append(("bumps", where, knock))
            self.bump_rattles.append(knock)

    def _gear_event(self, frame: Frame) -> None:
        self.gear_cooldown = max(0.0, self.gear_cooldown - DT)
        if self.prev_gear is None:  # first live frame: already in this gear, nothing shifted
            self.prev_gear = frame.gear
            return
        if frame.gear != self.prev_gear and frame.gear != 0 and self.gear_cooldown == 0.0:
            if self.g.gearbox == CONVERTER:
                # A powershift (CONVERTER_SHIFT): pushed as hard as the engine pulls.
                coasting, floored = CONVERTER_SHIFT
                strength = coasting + (floored - coasting) * min(1.0, max(0.0, frame.game_throttle))
                self._both("gear", Burst(CONVERTER_SHIFT_HZ, strength * self.g.gear, CONVERTER_SHIFT_DECAY,
                                         attack_s=CONVERTER_SHIFT_ATTACK, ducks=False))
            else:
                # The recorded shift on its own timeline (SHIFT_HITS); the bed ducks
                # under the lever's knock and again just ahead of the clack.
                for delay, strength, decay in SHIFT_HITS:
                    self._both("gear", Burst(SHIFT_HZ, strength * self.g.gear, decay, delay_s=delay, phase=0.25))
            self.gear_cooldown = SHIFT_COOLDOWN
            self.levels.events.append(t("변속 {gear}단", gear=frame.gear))
        if frame.gear != 0:
            self.prev_gear = frame.gear

    def _impact_event(self, frame: Frame, dt: float) -> None:
        self.impact_cooldown = max(0.0, self.impact_cooldown - dt)
        ax, _, az = frame.accel
        horizontal = math.hypot(ax, az)
        self.levels.max_accel = max(self.levels.max_accel, horizontal)
        if dt and self.prev_accel is not None and self.impact_cooldown == 0.0:
            px, _, pz = self.prev_accel
            jump = math.hypot(ax - px, az - pz)
            threshold = self.g.impact_threshold
            if jump > threshold:
                strength = min(1.0, (jump - threshold) / 30.0 + 0.4) * self.g.impact
                self._both("impact", Burst(50.0, strength, 0.06))
                self.impact_cooldown = 0.3
                self.levels.events.append(t("충돌 {accel} m/s²", accel=f"{jump:.0f}"))
        self.prev_accel = frame.accel

    def _detail(self, side: int) -> float:
        """How much rougher the road under that side is now than over the last few seconds, rooted (DETAIL_*)."""
        if self.rough_avg[side] < 1e-4:
            return 1.0
        low, high = DETAIL_RANGE
        return math.sqrt(min(high, max(low, self.roughness[side] / self.rough_avg[side])))

    def _cab_event(self, frame: Frame, dt: float) -> None:
        """How the cab sways on its mounts now, each side (CAB_SWAY), and how much it has been shaking (CAB_RATTLE)."""
        square = sum(a * a for a in frame.cabin_accel)
        if square > self.cab_shake or not dt:
            self.cab_shake = square
        else:
            self.cab_shake += (square - self.cab_shake) * (1.0 - math.exp(-dt / CAB_SETTLE_TAU))
        tilt = math.sqrt(square)
        strength = _ramp(tilt, CAB_SWAY) * self.g.bumps
        lean = frame.cabin_accel[2] / tilt if tilt else 0.0  # the roll's share, positive pushing left
        far = 1.0 - abs(lean) * (1.0 - CAB_FAR_SIDE)
        self.cab_sway = (strength * (1.0 if lean >= 0 else far), strength * (far if lean >= 0 else 1.0))

    def _changed(self, key: str, value: object) -> bool:
        """True when value differs from the last frame's (never on the first frame)."""
        old = self.prev_state.get(key, value)
        self.prev_state[key] = value
        return old != value

    def _both(self, effect: str, burst: Burst) -> None:
        self.bursts.append((effect, "C", burst))

    def _state_events(self, frame: Frame) -> None:
        g, log = self.g, self.levels.events

        for key, value, slot in (("throttle", frame.user_throttle, THROTTLE_TRIGGER),
                                 ("brake", frame.user_brake, BRAKE_TRIGGER)):
            floored = value >= 0.97
            if self._changed(f"{key}_floor", floored) and floored and g.pedal:
                self._both("pedal", Burst(130.0, PEDAL_FLOOR * g.pedal, 0.012, phase=0.25, trigger_slot=slot))
                log.append(t("페달 바닥"))

        if self._changed("park_brake", frame.park_brake) and g.park_brake:
            s = g.park_brake
            if frame.park_brake:
                # Set: the knob clunks, then the spring brakes vent in a hiss that swells
                # for about 0.15 s and takes most of a second to die (as recorded).
                self._both("park_brake", Burst(110.0, 1.0 * s, 0.02, phase=0.25))
                self._both("park_brake", NoiseBurst(250.0, 0.5 * s, 0.35, delay_s=0.03, attack_s=0.15))
            else:
                # Released: the knob thunks, and a short pop of air about 0.3 s later. The
                # knob's release clips (Zeemods Western Star and Freightliner) centre on
                # 105-170 Hz within 30-250 Hz and fall 10 dB in 40-50 ms; at 130 Hz, 0.83
                # keeps the felt peak of the 70 Hz thunk it replaces.
                self._both("park_brake", Burst(130.0, 0.83 * s, 0.035, phase=0.25))
                self._both("park_brake", NoiseBurst(250.0, 0.45 * s, 0.05, delay_s=0.28))
            log.append(t("파킹 브레이크 체결") if frame.park_brake else t("파킹 브레이크 해제"))

        if self._changed("trailer", frame.trailer_attached) and g.trailer:
            s = g.trailer
            if frame.trailer_attached:
                self._couple(s)
                log.append(t("트레일러 연결"))
            else:
                self._both("trailer", Burst(50.0, 0.9 * s, 0.05))
                self._both("trailer", NoiseBurst(250.0, 0.4 * s, 0.15, delay_s=0.15))
                log.append(t("트레일러 분리"))

        if frame.user_brake > AIR_BRAKE_ON:
            self.brake_held += DT
            self.brake_peak = max(self.brake_peak, frame.user_brake)
        elif frame.user_brake < AIR_BRAKE_OFF:
            if self.brake_held >= AIR_BRAKE_HOLD and g.air_brake and frame.air_pressure > 30.0:
                s = g.air_brake * (0.4 + 0.6 * self.brake_peak)
                self._both("air_brake", Burst(50.0, AIR_BRAKE_THUMP * s, 0.03, phase=0.25))
                self._both("air_brake", NoiseBurst(250.0, 0.5 * s, AIR_BRAKE_HISS_DECAY, delay_s=0.01, attack_s=0.04))
            self.brake_held = self.brake_peak = 0.0

        # The lamp flag toggles with every flash; the relay clicks on both edges.
        self.blinker_idle += DT
        for key, on, side in (("blinker_l", frame.blinker_left, "L"),
                              ("blinker_r", frame.blinker_right, "R")):
            if self._changed(key, on) and g.blinker:
                self.bursts.append(("blinker", side, Burst(BLINKER_HZ, (1.0 if on else 0.7) * g.blinker, BLINKER_DECAY,
                                                           phase=0.25)))
                if self.blinker_idle > 1.5:
                    log.append(t("방향지시등 좌") if key == "blinker_l" else t("방향지시등 우"))
                self.blinker_idle = 0.0

        if self._changed("engine_on", frame.engine_on) and g.engine_start:
            s = g.engine_start
            if frame.engine_on:
                peak = ENGINE_START_PEAK * s
                self._both("engine_start", NoiseBurst(110.0, 0.1 * s, 0.8, delay_s=0.05, attack_s=0.05))  # the starter
                engaging = Burst(115.0, peak * 10 ** (-7 / 20), 0.01, delay_s=0.08, phase=0.25)
                self._both("engine_start", engaging)
                for at, db in ENGINE_CRANK:  # compression knocks, low like the exhaust's
                    self._both("engine_start", Burst(70.0, peak * 10 ** (db / 20), 0.015, delay_s=at, phase=0.25))
                self._firings(ENGINE_CATCH, peak, ENGINE_CATCH_AT)
                log.append(t("시동"))
            else:
                self._firings(ENGINE_STOP, ENGINE_STOP_PEAK * s)
                log.append(t("시동 끔"))

        for i, toggle in enumerate(frame.shifter_toggle):
            if self._changed(f"shifter_{i}", toggle) and g.shifter:
                for delay, freq, strength, decay in SHIFTER_HITS[min(i, 1)]:
                    self._both("shifter", Burst(freq, strength * g.shifter, decay, delay_s=delay, phase=0.25))
                log.append(t("레인지 셀렉터") if i == 0 else t("스플리터"))

        previous = self.prev_state.get("game_events", frozenset())
        self.prev_state["game_events"] = frame.game_events
        for name in sorted(frame.game_events - previous):
            if g.game_events:
                s = g.game_events
                # two taps, each over in a few cycles so the pair stays two
                self._both("game_events", Burst(80.0, 1.0 * s, 0.015, phase=0.25))
                self._both("game_events", Burst(80.0, 1.0 * s, 0.015, delay_s=0.12, phase=0.25))
            log.append(t(GAME_EVENT_LABELS[name]) if name in GAME_EVENT_LABELS else name)

    def _couple(self, strength: float) -> None:
        """The fifth wheel's clatter as a trailer couples (COUPLE_*), different every time."""
        knocks, at = [], 0.0
        while at < COUPLE_S:
            knocks.append(at)
            at += self.rng.uniform(*COUPLE_GAP)
        main = self.rng.uniform(*COUPLE_MAIN)
        loudest = min(knocks, key=lambda k: abs(k - main))
        for at in knocks:
            level = COUPLE_LOUDEST if at == loudest else COUPLE_LOUDEST * self.rng.uniform(*COUPLE_KNOCKS)
            self._both("trailer", Burst(self.rng.uniform(*COUPLE_HZ), level * strength, COUPLE_DECAY, delay_s=at,
                                        phase=0.25))

    def _firings(self, firings: tuple[tuple[float, float], ...], peak: float, start: float = 0.0) -> None:
        """The engine's firings as it starts or stops: (s, dB against peak) each."""
        for at, db in firings:
            self._both("engine_start", Burst(ENGINE_FIRING_HZ, peak * 10 ** (db / 20), ENGINE_FIRING_DECAY,
                                             delay_s=start + at, phase=0.25))

    def _novelty(self, key: str, value: float, scale: float, dead: float) -> float:
        """1 while value moves by dead + scale or more within about CHANGE_TAU; 0 for moves inside dead."""
        ref = self.steady_ref.setdefault(key, value)
        self.steady_ref[key] = ref + (value - ref) * CHANGE_K
        return min(1.0, max(0.0, abs(value - ref) - dead) / scale)

    def _steady_gain(self, bed: str, novelty: float) -> float:
        """Full as the bed's input moves (novelty 1), settling toward the floor over FADE_TAU while it holds."""
        held = self.fresh[bed] = max(novelty, self.fresh[bed] * FADE_K)
        floor = 1.0 - min(1.0, max(0.0, self.g.steady_fade))
        return floor + (1.0 - floor) * held

    def _learn_yaw_sign(self, frame: Frame) -> None:
        """Weigh one frame's turn against its wheels and flip yaw_sign once they clearly disagree."""
        if abs(frame.yaw_rate) < 0.05 or abs(frame.speed) < 1.0:
            return
        rims = [(w.pos_x, abs(w.velocity) * TAU * w.radius) for w in frame.wheels if w.on_ground]
        left = [rim for x, rim in rims if x < -0.3]
        right = [rim for x, rim in rims if x > 0.3]
        if not left or not right:
            return
        spread = sum(right) / len(right) - sum(left) / len(left)  # reversing, the inside runs faster
        self.yaw_evidence += frame.yaw_rate * spread * math.copysign(1.0, frame.speed)
        self.yaw_evidence = max(-5 * YAW_EVIDENCE, min(5 * YAW_EVIDENCE, self.yaw_evidence))
        sign = math.copysign(1.0, self.yaw_evidence)
        if abs(self.yaw_evidence) > YAW_EVIDENCE and sign != self.yaw_sign:
            self.yaw_sign = sign
            self.levels.events.append(t("회전 각속도 부호 뒤집음 (바퀴 속도 기준)"))

    def _wheel_spin(self, frame: Frame) -> float:
        """Share of grounded driven wheels turning clearly faster than their own spot of road moves.

        In a turn every wheel rolls at its own speed: in a tight one at walking
        pace the outer front wheel runs a third faster than the truck, and on a
        truck whose front axle is driven that alone made the grips buzz with
        wheelspin for 3 s of a drive (2026-11-02). So each wheel is held to
        where its contact patch goes as the truck turns at its yaw rate.
        """
        speed = abs(frame.speed)
        grounded = [w for w in frame.wheels if w.on_ground and w.powered]
        if not grounded:
            return 0.0
        spinning = 0
        yaw = frame.yaw_rate * self.yaw_sign
        for wheel in grounded:
            # the patch's velocity: forward speed plus the turn's share (x right, z back)
            ground = math.hypot(frame.speed + yaw * wheel.pos_x, yaw * wheel.pos_z)
            excess = abs(wheel.velocity) * TAU * wheel.radius - ground
            # 25% clears the outer wheels of a tight turn; 1 m/s the creep off the line
            if excess > max(1.0, 0.25 * speed):
                spinning += 1
        return spinning / len(grounded)

    def _wheel_lock(self, frame: Frame) -> float:
        speed = abs(frame.speed)
        if speed < 3.0:
            return 0.0
        grounded = [w for w in frame.wheels if w.on_ground]
        if not grounded:
            return 0.0
        locked = 0
        for wheel in grounded:
            surface_speed = abs(wheel.velocity) * TAU * wheel.radius
            if (speed - surface_speed) / speed > 0.3:
                locked += 1
        return locked / len(grounded)

    # -- block rendering ------------------------------------------------------

    def render(self, frame: Frame | None) -> Block:
        self.levels.events = []
        live = frame is not None and frame.live
        g = self.g

        engine_amp = road_l = road_r = rumble_l = rumble_r = spin_amp = retarder_amp = jake_amp = 0.0
        trigger_amp = squeal_amp = engine_harsh = engine_uneven = 0.0
        engine_freq = rumble_freq = 40.0
        retarder_freq = RETARDER_HZ[0]
        trigger_mode = "-"
        lock = 0.0
        if live and not self.was_live:
            self._rebaseline()
        self.was_live = live
        if live:
            dt = self._frame_dt(frame)
            if dt is not None:  # the same frame read again holds no new movement
                self._wheel_events(frame, dt)
                self._impact_event(frame, dt)
                self._cab_event(frame, dt)
            self._gear_event(frame)
            self._state_events(frame)

            # Brake squeal: light braking while rolling to a stop.
            speed = abs(frame.speed)
            braking = max(frame.user_brake, frame.game_brake)
            if braking > 0.08 and 0.3 < speed < 4.0:
                squeal_amp = g.squeal * 0.35 * math.sqrt(1.0 - speed / 4.0)

            load = max(frame.game_throttle, 0.0)
            rpm_frac = min(1.0, frame.rpm / frame.rpm_max)
            # cruise control trims the load by a few percent all the time
            engine_moved = max(self._novelty("load", load, 0.15, 0.05), self._novelty("rpm", rpm_frac, 0.06, 0.02))
            if frame.engine_on and frame.rpm > 100:
                # Firing frequency of a 4-stroke engine: rpm/60 * cylinders/2.
                engine_freq = max(25.0, min(140.0, frame.rpm / 60.0 * g.cylinders / 2.0))
                standing = max(0.0, 1.0 - speed / ENGINE_STANDING_SPEED) * (1.0 - load)
                engine_amp = (g.engine * (0.25 + 0.55 * load + 0.2 * rpm_frac)
                              * (1.0 + (ENGINE_STANDING - 1.0) * standing)
                              * self._steady_gain("engine", engine_moved))
                engine_harsh = ENGINE_HARSH * load
                engine_uneven = max(ENGINE_UNEVEN_FLOOR, 1.0 - 2.0 * load - rpm_frac)

            speed_factor = min(1.0, abs(frame.speed) / 25.0) ** 0.7
            sides = ([w for w in frame.wheels if w.pos_x < 0 and w.on_ground],
                     [w for w in frame.wheels if w.pos_x >= 0 and w.on_ground])
            # A new surface, rougher ground or a change of pace brings the road back. Off the
            # pedals at speed the truck drifts up to 3.6 km/h from where it was 2 s before
            # (90th percentile of the developer's 220 mile haul); that is not a new pace.
            road_moved = self._novelty("speed", speed, 2.0, 1.0)
            textures = []
            for side, wheels in enumerate(sides):
                surface = side_surface(wheels)
                self.texture[side].tune(surface, speed, self.roughness[side], self.cab_shake)
                rough = self.roughness[side]
                cab_low, cab_full = CAB_TEXTURE
                cab = min(1.0, max(0.0, (math.sqrt(self.cab_shake) - cab_low) / (cab_full - cab_low)))
                texture = min(1.0, surface.level * speed_factor + min(1.0, (rough / ROUGH_FULL) ** ROUGH_POWER) + cab)
                # what counts as rougher ground stays in proportion (ROUGH_NOVELTY): squared, the
                # highway's every jolt took the road out of its fade
                road_moved = max(road_moved, self._novelty(f"road_{side}", min(1.0, surface.level * speed_factor
                                                                                + ROUGH_NOVELTY * rough), 0.15, 0.05))
                textures.append(texture)
            road_gain = g.road * self._steady_gain("road", road_moved)
            road_l, road_r = (road_gain * texture * self._detail(side) for side, texture in enumerate(textures))
            rumble_freq = max(25.0, min(120.0, abs(frame.speed) / RUMBLE_SPACING))
            trailer_sides = ([w for w in frame.trailer_wheels if w.pos_x < 0 and w.on_ground],
                             [w for w in frame.trailer_wheels if w.pos_x >= 0 and w.on_ground])
            rumble = [0.0, 0.0]
            for hold, groups, feel in ((self.rumble_hold[0], sides, 1.0),
                                       (self.rumble_hold[1], trailer_sides, TRAILER_FEEL)):
                for side, wheels in enumerate(groups):
                    if any("rumble" in w.substance.lower() for w in wheels):
                        hold[side] = RUMBLE_HOLD
                    else:
                        hold[side] = max(0.0, hold[side] - DT)
                    if hold[side] > 0:
                        rumble[side] = max(rumble[side], feel)
            strip = g.rumble * min(1.0, abs(frame.speed) / 15.0) ** 0.5 if abs(frame.speed) > 1.0 else 0.0
            rumble_l, rumble_r = strip * rumble[0], strip * rumble[1]

            # Wheelspin: driven wheels outrunning the truck under power (ice, mud, a hard launch).
            self._learn_yaw_sign(frame)
            powered = frame.engine_on and max(frame.user_throttle, frame.game_throttle) > 0.05
            spin = self._wheel_spin(frame) if powered else 0.0
            self.spin = max(spin, self.spin - DT / 0.15)  # let go over 150 ms, not on one frame
            if self.spin > 0:
                spin_amp = g.wheelspin * min(1.0, 0.4 + self.spin)
            if frame.wheels:
                self.levels.surface = frame.wheels[0].substance

            rolling = min(1.0, speed / 5)
            if frame.retarder and frame.retarder_steps:
                retarder_amp = g.retarder * frame.retarder / frame.retarder_steps * rolling
                low, high = RETARDER_HZ
                retarder_freq = low + (high - low) * min(1.0, speed / 25.0)
            if frame.motor_brake and frame.engine_on and frame.rpm > 100 and load < 0.05:
                jake_amp = g.retarder * (0.3 + 0.3 * rpm_frac) * rolling

            # One trigger channel: follow whichever pedal the player is pressing.
            # A wheel lock is a warning and never fades.
            lock = self._wheel_lock(frame) if frame.user_brake > 0.05 else 0.0
            # the finger on the trigger is never quite still
            throttle_moved = self._novelty("throttle", frame.user_throttle, 0.1, 0.03)
            brake_moved = self._novelty("brake", frame.user_brake, 0.1, 0.03)
            if frame.user_brake > 0.05 and frame.user_brake >= frame.user_throttle:
                trigger_mode = "brake"
                if lock > 0:
                    trigger_amp = g.trigger * min(1.0, 0.6 + lock)
                else:
                    # 0.25 x pedal left an ordinary stop at 1% of full scale. Now a stop is felt,
                    # at about 0.6 of the steady throttle on the same pedal (as strong as a pulsed
                    # one), and flat out it reaches the lock warning's floor.
                    trigger_amp = (g.trigger * (0.1 + 0.5 * frame.user_brake) * speed_factor
                                   * self._steady_gain("trigger", brake_moved))
            elif frame.user_throttle > 0.05 and frame.engine_on:
                trigger_mode = "throttle"
                trigger_amp = (g.trigger * (0.2 + 0.6 * frame.user_throttle)
                               * self._steady_gain("trigger", throttle_moved))

        if lock > 0 and not self.was_locked:
            self.lock_phase = 0.0  # the warning starts with a pulse, not up to half a cycle late
        self.was_locked = lock > 0
        if trigger_mode != "-":  # after letting go, its tail stays on that pedal's trigger
            self.trigger_slot = BRAKE_TRIGGER if trigger_mode == "brake" else THROTTLE_TRIGGER
        sway_k = 1.0 - math.exp(-DT / CAB_SWAY_TAU)
        for side, want in enumerate(self.cab_sway if live else (0.0, 0.0)):
            self.sway[side] += (want - self.sway[side]) * sway_k
            if not want and self.sway[side] < 1e-4:
                self.sway[side] = 0.0
        sway_l, sway_r = self.sway
        routes = {name: route_of(g, name) for name in TOGGLEABLE}
        # each vibration scaled for how strongly each motor gives back its frequency
        eq = self.eq.pair
        eq_engine, eq_retarder, eq_rumble = eq(engine_freq), eq(retarder_freq), eq(rumble_freq)
        eq_rumble_half = eq(rumble_freq / 2)
        eq_road = (eq(self.texture[0].pitch), eq(self.texture[1].pitch))
        eq_spin, eq_squeal, eq_sway = eq(self.spin_noise.filter.center), eq(Squeal.HZ), eq(CAB_SWAY_HZ)
        eq_trigger = eq(110.0 if trigger_mode == "brake" and lock > 0 else 100.0)
        eq_bursts = [eq(burst.freq) for _, _, burst in self.bursts]
        trigger, left, right = [], [], []
        heard: set[int] = set()  # the TRIGGER_SLOTS something played in
        p = self.prev_amp
        for n in range(SUBFRAMES):
            k = (n + 1) / SUBFRAMES
            e = p["engine"] + (engine_amp - p["engine"]) * k
            rl = p["road_l"] + (road_l - p["road_l"]) * k
            rr = p["road_r"] + (road_r - p["road_r"]) * k
            ul = p["rumble_l"] + (rumble_l - p["rumble_l"]) * k
            ur = p["rumble_r"] + (rumble_r - p["rumble_r"]) * k
            sp = p["spin"] + (spin_amp - p["spin"]) * k
            rt = p["retarder"] + (retarder_amp - p["retarder"]) * k
            jb = p["jake"] + (jake_amp - p["jake"]) * k
            eh = p["harsh"] + (engine_harsh - p["harsh"]) * k
            tr = p["trigger"] + (trigger_amp - p["trigger"]) * k
            sq = p["squeal"] + (squeal_amp - p["squeal"]) * k
            wl = p["sway_l"] + (sway_l - p["sway_l"]) * k
            wr = p["sway_r"] + (sway_r - p["sway_r"]) * k

            out = [0.0] * 5  # left grip, right grip, then the TRIGGER_SLOTS: events
            bed = [0.0] * 5  # continuous effects, ducked under the events
            firing, fire_gain = self.engine_osc.phase, self.firing_gain
            w = TAU * firing
            engine = e * fire_gain * (self.engine_osc.next(engine_freq) + 0.3 * math.sin(2 * w)
                                             + eh * math.sin(3 * w))
            if self.engine_osc.phase < firing:  # a cycle done: the next cylinder fires
                self._next_firing(engine_uneven)
            _mix(bed, routes["engine"], engine, engine, engine, eq_engine)
            retarder = rt * self.retarder_osc.next(retarder_freq)
            _mix(bed, routes["retarder"], retarder, retarder, retarder, eq_retarder)
            if jb:
                # on the engine's own firing, knock peak on its crest: on a phase of their
                # own the two could cancel, the same frequency at once
                jake = jb * jake_pulse((firing - 0.25 + JAKE_PEAK) % 1.0)
                _mix(bed, routes["retarder"], jake, jake, jake, eq_engine)
            # Independent noises: 0.7 keeps their sum near one side's strength.
            noise_l, noise_r = rl * self.texture[0].next(), rr * self.texture[1].next()
            (grip_l, trig_l), (grip_r, trig_r) = eq_road
            _mix(bed, routes["road"], noise_l * grip_l, noise_r * grip_r,
                 0.7 * (noise_l * trig_l + noise_r * trig_r))
            if ul or ur:
                # the half-rate part takes the eq of its own frequency
                (strip_l, half_l), (strip_r, half_r) = self._strip(0, rumble_freq), self._strip(1, rumble_freq)
                strip_l, half_l, strip_r, half_r = ul * strip_l, ul * half_l, ur * strip_r, ur * half_r
                _mix(bed, routes["rumble"], strip_l, strip_r, 0.7 * (strip_l + strip_r), eq_rumble)
                _mix(bed, routes["rumble"], half_l, half_r, 0.7 * (half_l + half_r), eq_rumble_half)
            if sp:
                # Low grinding noise, chattering as the tyres hop between grip and slip.
                # 2x: noise is spiky, and this has to stand out over a floored engine.
                grind = 2.0 * sp * self.spin_noise.next() * (0.55 + 0.45 * self.spin_chatter.next(13.0))
                _mix(bed, routes["wheelspin"], grind, grind, grind, eq_spin)

            if trigger_mode == "brake" and lock > 0:
                t = tr * self._lock_chop() * self.trigger_osc.next(110.0)
            elif trigger_mode == "throttle":
                # 100 Hz carrier, optionally pulsed with the engine's own firings: on
                # their beat and each as strong as that firing, so the idle's lope and
                # the firing-to-firing differences come through the pedal too.
                depth = min(1.0, max(0.0, g.throttle_pulse))
                pulse = (1.0 - depth * 0.5 * (1.0 - math.sin(w))) * (1.0 + depth * (fire_gain - 1.0))
                t = tr * pulse * self.trigger_osc.next(100.0)
            else:
                t = tr * self.trigger_osc.next(100.0)
            _mix(bed, routes["trigger"], t, t, t, eq_trigger, self.trigger_slot)
            if sq:
                squeal = sq * self.squeal.next()
                _mix(out, routes["squeal"], squeal, squeal, squeal, eq_squeal, BRAKE_TRIGGER)
            else:
                self.squeal.restart()
            if wl or wr:
                # one cab, one tone: the same swing on both grips, leaning as the cab does
                c = self.sway_osc.next(CAB_SWAY_HZ)
                _mix(out, routes["bumps"], wl * c, wr * c, 0.7 * (wl + wr) * c, eq_sway)
            drive = [0.0] * 5
            for (effect, side, burst), burst_eq in zip(self.bursts, eq_bursts):
                slot = burst.trigger_slot
                if effect not in DUCK_EXEMPT and burst.ducks:
                    d = burst.level
                    _mix(drive, routes[effect], 0.0 if side == "R" else d, 0.0 if side == "L" else d, d, FLAT, slot)
                b = burst.next()
                _mix(out, routes[effect], 0.0 if side == "R" else b, 0.0 if side == "L" else b, b, burst_eq, slot)
            for slot in TRIGGER_SLOTS:
                if out[slot] or bed[slot]:
                    heard.add(slot)
            for mix in (out, bed, drive):  # one trigger channel from here on
                mix[BOTH_TRIGGERS] += mix[BRAKE_TRIGGER] + mix[THROTTLE_TRIGGER]
            for i, cut in enumerate(self.cuts):
                target = min(1.0, drive[i] / DUCK_FULL) * g.ducking
                if target > self.duck[i]:
                    self.duck[i] += (target - self.duck[i]) * DUCK_ATTACK
                else:
                    self.duck[i] = max(target, self.duck[i] * DUCK_RELEASE)
                out[i] += bed[i] * (1.0 - self.duck[i])
                if cut:
                    out[i] = cut.next(out[i])

            left.append(self._out(out[0], self.g.grip_level, CEILING["grip"]))
            right.append(self._out(out[1], self.g.grip_level, CEILING["grip"]))
            trigger.append(self._out(out[2], self.g.trigger_level, CEILING["trigger"]))
        self.prev_amp = {"engine": engine_amp, "road_l": road_l, "road_r": road_r,
                         "rumble_l": rumble_l, "rumble_r": rumble_r, "spin": spin_amp, "retarder": retarder_amp,
                         "jake": jake_amp, "trigger": trigger_amp, "squeal": squeal_amp, "harsh": engine_harsh,
                         "sway_l": sway_l, "sway_r": sway_r}
        self.bursts = [entry for entry in self.bursts if not entry[2].done]

        lv = self.levels
        lv.engine, lv.road_left, lv.road_right = engine_amp, road_l, road_r
        lv.trigger, lv.trigger_mode = trigger_amp, trigger_mode
        if not any(trigger):
            return Block(trigger=trigger, left_grip=left, right_grip=right, route=Route.MUTE)
        # the 0x57 route stays BOTH, as Flydigi's engine sends it: the side is picked by Block.side
        return Block(trigger=trigger, left_grip=left, right_grip=right, route=Route.BOTH,
                     side=self._trigger_side(heard))

    def _trigger_side(self, heard: set[int]) -> Route:
        """Where this block's trigger channel plays: one pedal's trigger when only its effects
        played on the channel, else both; with nothing in the mix (the trigger's resonance cut
        ringing out), where it played last."""
        if heard == {BRAKE_TRIGGER}:
            self.trigger_side = Route.LEFT
        elif heard == {THROTTLE_TRIGGER}:
            self.trigger_side = Route.RIGHT
        elif heard:
            self.trigger_side = Route.BOTH
        return self.trigger_side

    def _strip(self, side: int, freq: float) -> tuple[float, float]:
        """One sample of a side's strip (RUMBLE_SHAPE): the grooves with their harmonics, and the half-rate part."""
        w = TAU * self.rumble_osc[side].phase
        half, second, third = RUMBLE_SHAPE
        grooves = self.rumble_osc[side].next(freq) + second * math.sin(2 * w) + third * math.sin(3 * w)
        return RUMBLE_LEVEL * grooves, RUMBLE_LEVEL * half * self.rumble_half[side].next(freq / 2)

    def _lock_chop(self) -> float:
        """Wheel-lock gate: on for the first half of each LOCK_RATE cycle, with LOCK_EDGE ramps."""
        p = self.lock_phase
        self.lock_phase = (p + LOCK_RATE / SAMPLE_RATE) % 1.0
        if p >= 0.5:
            return 0.0
        edge = LOCK_EDGE * LOCK_RATE
        return min(1.0, p / edge, (0.5 - p) / edge)

    def _next_firing(self, uneven: float) -> None:
        """Strength of the next cylinder's firing: its own share of the unevenness, which grows
        towards idle (a V8's stays put), and a little difference from one cycle to the next."""
        cylinders = max(1, min(len(self.cylinder_offsets), self.g.cylinders))
        self.cylinder = (self.cylinder + 1) % cylinders
        used = self.cylinder_offsets[:cylinders]
        mean = sum(used) / cylinders
        offset = used[self.cylinder] - mean
        if cylinders == 8:
            spread = math.sqrt(sum((o - mean) ** 2 for o in used) / cylinders)
            # as strong overall as an even engine, so the engine level stays where it was
            imbalance = ((1.0 + ENGINE_V8_PATTERN * offset / spread) / math.sqrt(1.0 + ENGINE_V8_PATTERN ** 2)
                         - 1.0)
        else:
            imbalance = ENGINE_IMBALANCE * uneven * offset
        self.firing_gain = (1.0 + imbalance) * (1.0 + ENGINE_JITTER * self.rng.uniform(-1.0, 1.0))

    def _out(self, x: float, channel_level: float, ceiling: float) -> float:
        # Soft limit so stacked effects saturate smoothly instead of clipping:
        # MAX_SAMPLE per unit while quiet, never past the channel's ceiling.
        return math.tanh(x * self.g.master * channel_level * MAX_SAMPLE / ceiling) * ceiling


def _mix(out: list[float], route: str, left: float, right: float, trigger: float,
         eq: tuple[float, float] = FLAT, slot: int = BOTH_TRIGGERS) -> None:
    """Add one effect's sample to the channels its route selects, scaled by its (grip, trigger) eq;
    on the trigger, in the slot of the pedal it belongs to (TRIGGER_SLOTS)."""
    if route != TRIGGER:
        out[0] += left * eq[0]
        out[1] += right * eq[0]
    if route != GRIP:
        out[slot] += trigger * eq[1]


def _ramp(x: float, points: tuple[tuple[float, float], ...]) -> float:
    """Straight lines through (x, y) points: the first y below them, the last above."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]
