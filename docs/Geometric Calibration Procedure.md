# Geometric Calibration Procedure
**Instrument:** Gonioreflectometer (GRControl)  
**Based on:** Chapter 5.1 – Geometric Calibration  
**Prerequisites:** Mechanical assembly complete. ESP32 firmware flashed. GRControl software running (`python run.py`, open `http://localhost:8000`).  
**No skin sample required.** All procedures use passive optical/mechanical fixtures only.

---

## Equipment and Fixtures Required

Gather all items before starting. Do not begin any procedure with substitutes.

| Item | Purpose | Used in |
|---|---|---|
| Optical alignment target (printed crosshair reticle or fine-tipped laser pointer on a jig) | Marks the intended sample aperture position to verify both arcs rotate about the same physical point | §2, §3 |
| Independent angular reference standard (digital protractor, rotary calibration stage, or temporarily mounted calibrated encoder) | Provides ground-truth angle measurement independent of the onboard encoders | §3 |
| Flat, non-specular calibration coupon (matte white card or diffuse reflectance standard) | Receives illumination and camera view to verify spot co-location | §3 (shared-axis), §4 |
| Scale reference (ruler or printed grid) | Provides a known length in the sample plane for spot-size measurement | §4 |
| Calibration log sheet or spreadsheet | Records all raw values, deviations, and repeat measurements | All |

---

## Procedure Overview

| # | Procedure | Output |
|---|---|---|
| 1 | Software setup and connection check | Software confirmed operational |
| 2 | Zero-angle reference establishment | `homeCamera` and `homeLed` offsets stored in ESP32 |
| 3a | Angular accuracy verification — LED arc | Commanded-vs-true deviation table and plot |
| 3b | Angular accuracy verification — camera arc | Commanded-vs-true deviation table and plot |
| 4 | Shared-axis alignment verification | Spatial drift (mm) vs. angle for both arcs |
| 5 | Sample spot size and FOV characterization | Spot diameter and FOV diameter vs. angle for all LED/camera positions |

Complete all procedures **in order**. Each depends on the result of the previous one.

---

## Procedure 1 — Software Setup and Connection Check

**Goal:** Confirm the GRControl software can communicate with the ESP32, the camera is accessible, and encoder telemetry is live before any mechanical work begins.

### Steps

1. Start the backend:
   ```
   python run.py
   ```
   Open `http://localhost:8000` in a browser.

2. In the **Connection** sidebar, select the correct serial port (click **↻ Refresh ports**) or enter the ESP32's IP address in the WiFi/TCP tab. Click **Connect**. The top-bar indicator should turn green and show "Connected (serial)" or "Connected (tcp)".

3. Observe the **Encoder Readings** card. Within 1–2 seconds, all four encoder fields (Motor 1, Motor 2, Camera Final OME85, LED Arc Final AS5600 #3) should show live numeric values updating at ~10 Hz. If any field shows "—", investigate the corresponding encoder wiring before continuing.

4. Manually turn each arc a small amount by hand (motor power off) and confirm the corresponding encoder value changes in the display. If the OME85 camera encoder reads erratically, refer to the TXS0108E CRC-corruption known issue — position data is majority-voted and is reliable even when CRC fails.

5. Open the **Camera Control** card. Click **Open Camera**. The camera status indicator should turn green. Click **▶ Live** to start the MJPEG preview. Confirm a live image appears. Note the current gain value (default 10) and exposure.

6. In the **Encoder Readings** card, click **Measure encoder jitter** with the default 10 s window while both arcs are held stationary. Record the peak-to-peak values:
   - Camera OME85: expected < 0.05° at rest (noise floor)
   - LED arc AS5600 #3: expected < 0.15° at rest

   These values set the practical resolution floor for all subsequent angular measurements.

**Pass criterion:** Both arcs report live encoder values. Camera opens and shows a live preview. Jitter values are within the noise-floor range above.

---

## Procedure 2 — Zero-Angle Reference Establishment

**Goal:** Define the true physical 0° position (sample-plane normal direction) for both arcs and store it as the home offset in the ESP32. Repeat three times to quantify repeatability.

**Concept:** The OME85 and AS5600 #3 report angles relative to an arbitrary mechanical zero set at installation. This procedure overwrites that with a meaningful reference: the position at which each arc points directly toward the sample normal (normal incidence/observation = 0°).

### What "0°" means physically

- **Camera arc at 0°:** The camera arm points directly at the sample aperture along the surface normal — i.e., the optical axis of the camera is perpendicular to the sample plane.
- **LED arc at 0°:** The LED arc arm is also at the surface normal — the LED directly above (or at normal incidence to) the sample aperture.

### Steps

#### Preparation

1. Place the **optical alignment target** (crosshair reticle) at the intended sample aperture position on the instrument's base plate. Secure it so it cannot move during the procedure.

2. Ensure both arc motors are powered. In the GRControl **Manual Motor Control** card, confirm the arcs respond to jog commands before proceeding.

#### Setting the camera arc home (repeat ×3)

**Repetition 1:**

3. Using the **Manual Motor Control** card, jog Motor 1 (camera) in small increments (0.5° or less) while watching the live camera preview. The camera's field of view center should align with the crosshair of the alignment target. Take your time — small jog steps give fine control. Use speed 10–20% for precision.

4. When the camera's center crosshair (or the center of the field of view) is precisely aligned to the target, stop.

5. Read and record the current **OME85 encoder value** displayed in the Encoder Readings card. Label this `OME85_raw_0deg_rep1`.

6. In the sidebar **Reference / Home** section, click **Home Camera**. The ESP32 will store the current encoder position as the camera's 0° reference. The camera encoder display should now read approximately 0.0000°.

7. Verify by jogging the camera arc a few degrees away and then commanding a **Move to absolute angle** of `0°` (Motor 1, precise positioning enabled). Confirm it returns to the alignment position and the encoder reads within 0.05° of 0°.

**Repetitions 2 and 3:**

8. Jog the camera arc ~10° away from the home position.

9. Repeat steps 3–7 for repetitions 2 and 3. Record `OME85_raw_0deg_rep2` and `OME85_raw_0deg_rep3` (the raw values before clicking Home each time).

10. Calculate the spread across the three home-setting attempts:
    ```
    repeatability = max(rep1, rep2, rep3) − min(rep1, rep2, rep3)
    ```
    Record this as the **camera zero-reference repeatability** (degrees). This is your lower bound on camera angular accuracy.

#### Setting the LED arc home (repeat ×3)

11. Remove power from the camera arc motor (or leave it at home). Now work on the LED arc.

12. Jog Motor 2 (LED arc) in small increments while watching the alignment target. The illumination spot from the active LED should center on the target crosshair. Turn on a single LED (e.g., L1 at brightness 128) using the **LED Control** card to make the spot visible.

13. When the illumination spot center is precisely on the target, stop.

14. Read and record the **AS5600 #3 encoder value**. Label this `AS5600_raw_0deg_rep1`.

15. Click **Home LED** in the sidebar. Verify the LED arc encoder now reads approximately 0.0000°.

16. Verify by jogging away ~10° and returning to absolute 0°. Confirm the spot re-centers on the target within 0.1°.

17. Repeat steps 12–16 twice more. Record `AS5600_raw_0deg_rep2` and `AS5600_raw_0deg_rep3`.

18. Calculate the **LED arc zero-reference repeatability** the same way as step 10.

#### Pass criterion

| Metric | Target |
|---|---|
| Camera zero-reference repeatability (3 reps) | ≤ 0.10° |
| LED arc zero-reference repeatability (3 reps) | ≤ 0.15° |

If either value exceeds its target, the mounting of the corresponding encoder or its magnet/ring-scale should be investigated for looseness before continuing.

#### Record to keep

```
Camera arc home — repeatability across 3 attempts: _______ °
LED arc home    — repeatability across 3 attempts: _______ °
Date / operator: _______________
```

---

## Procedure 3a — Angular Accuracy Verification: LED Arc

**Goal:** Verify that the angle commanded (and reported by AS5600 #3) matches the true physical angle across the full LED arc operating range (0° to 60°, discrete slit positions).

**Prerequisite:** Procedure 2 complete. LED arc home is set.

### Test positions

Use the discrete slit positions defined for the LED arc in your instrument spec (Section 4.3.1.1). Typical positions: **0°, 10°, 20°, 30°, 40°, 50°, 60°**. Add any additional intermediate positions from the spec if applicable.

### Steps

1. Attach the **angular reference standard** to the LED arc so it can be read at each commanded position. (If using a digital protractor or a temporary calibrated encoder, mount it now with the instrument at 0° as the reference.)

2. Zero the angular reference standard at 0° (consistent with the home reference just established).

3. In the GRControl **Manual Motor Control** card, command Motor 2 to each test position in sequence using **Move to absolute angle** with **Precise positioning enabled**.

4. At each position, once the motor has stopped and the encoder reading has stabilized (1–2 seconds):
   a. Record the **commanded angle** (the value you entered).
   b. Record the **AS5600 #3 encoder value** shown in the display.
   c. Record the **independent angular reference standard reading** (the ground-truth angle).
   d. Calculate: `deviation = encoder_reported − true_angle`.

5. Repeat the full sweep twice more (three total sweeps), approaching each position from the same direction each time (always moving in the increasing-angle direction) to avoid mixing backlash contributions.

6. Also run one sweep approaching from the decreasing-angle direction. The difference between the two approach directions at each position quantifies **backlash** at that angle.

### Data table (one per sweep direction)

| Commanded (°) | AS5600 #3 reported (°) | True angle (ref std) (°) | Deviation (°) |
|---|---|---|---|
| 0 | | | |
| 10 | | | |
| 20 | | | |
| 30 | | | |
| 40 | | | |
| 50 | | | |
| 60 | | | |

### Pass criterion

| Metric | Target |
|---|---|
| Maximum absolute deviation at any position | ≤ 0.10° (consistent with OME85-class accuracy goal) |
| Repeatability across 3 sweeps at any position | ≤ 0.15° |
| Backlash (difference between approach directions) | Record and document; no pass/fail threshold defined yet |

If a **monotonically growing deviation** is observed (error increases with angle), this indicates a belt-thickness-induced gear-ratio error (Section 8.5). Document it as a known systematic bias with its magnitude at each angular position. Do not simply re-home to mask it.

---

## Procedure 3b — Angular Accuracy Verification: Camera Arc

**Goal:** Same as 3a but for the camera arc, over its operating range (20° to 55°, discrete slit positions).

**Prerequisite:** Procedure 2 complete. Camera arc home is set.

### Test positions

Use the discrete slit positions from Section 4.3.2.1. Typical positions: **20°, 25°, 30°, 35°, 40°, 45°, 50°, 55°**. Include 0° as an additional reference point even though it is outside the nominal operating range.

### Steps

1. Attach (or reposition) the angular reference standard to the camera arc.

2. Repeat the same measurement protocol as Procedure 3a steps 2–6, using Motor 1 (camera) and the **OME85 encoder value** as the reported angle.

### Data table (one per sweep direction)

| Commanded (°) | OME85 reported (°) | True angle (ref std) (°) | Deviation (°) |
|---|---|---|---|
| 0 | | | |
| 20 | | | |
| 25 | | | |
| 30 | | | |
| 35 | | | |
| 40 | | | |
| 45 | | | |
| 50 | | | |
| 55 | | | |

### Pass criterion

| Metric | Target (per Table 4.1) |
|---|---|
| Maximum absolute deviation at any position | ≤ 0.10° (OME85 static angular error spec) |
| Repeatability across 3 sweeps at any position | ≤ 2 LSB (OME85 repeatability spec; 1 LSB ≈ 360°/131072 ≈ 0.00275°, so ≤ 0.0055°) |

Note: the ±2 LSB repeatability target is very tight. If the jitter measurement from Procedure 1 already showed peak-to-peak values above this at rest, the dominant noise source is the encoder read noise, not arc mechanical repeatability.

---

## Procedure 4 — Shared-Axis Alignment Verification

**Goal:** Verify that the LED arc and camera arc rotate about the same physical vertical axis. Any offset between the two centers of rotation causes the illuminated spot and the camera's field of view to drift relative to each other as either arc moves.

**Prerequisite:** Both zero-angle references established (Procedure 2). Camera is open and live preview is running.

### Part A — LED arc sweep

1. Fix the **optical alignment target** securely at the sample aperture. It must not move for the duration of this procedure.

2. Place the **flat calibration coupon** directly behind (or on top of) the alignment target so the LED illumination is visible on it.

3. In the LED Control card, turn on one LED at a modest brightness (100–150) so the illuminated spot is clearly visible in the camera's live preview.

4. Set the camera arc to a fixed reference position (e.g., 45°) and lock it there (do not move it during Part A).

5. Command the LED arc (Motor 2) to each of its operating positions: 0°, 10°, 20°, 30°, 40°, 50°, 60°. At each position:
   a. In the live camera preview, note whether the center of the illuminated spot is still aligned with the crosshair on the alignment target.
   b. If a scale reference (ruler/grid) is placed at the sample plane, read off the displacement of the spot center from the target in millimeters.
   c. Record: `LED arc angle (°)` | `spot displacement from target (mm)`.

6. If no scale reference is in the camera field of view, use pixel counting: measure the spot-center displacement in pixels and convert using a known reference dimension (e.g., the target crosshair gap if its physical size is known).

### Part B — Camera arc sweep

7. Return the LED arc to 0° and fix it there.

8. Command the camera arc (Motor 1) to each of its operating positions: 20°, 25°, 30°, 35°, 40°, 45°, 50°, 55°. At each position:
   a. Observe whether the camera's field-of-view center (the crosshair you would see in the middle of the image) remains aligned with the alignment target.
   b. Use an external camera or the instrument's own image (if the field of view is wide enough to see the target) to assess alignment. Record the displacement in mm.
   c. Record: `camera arc angle (°)` | `field-of-view center displacement from target (mm)`.

### Data tables

**Part A — LED arc rotation (camera fixed at reference position):**

| LED arc angle (°) | Spot displacement from target (mm) | Direction of drift |
|---|---|---|
| 0 | 0 (reference) | — |
| 10 | | |
| 20 | | |
| 30 | | |
| 40 | | |
| 50 | | |
| 60 | | |

**Part B — Camera arc rotation (LED arc fixed at 0°):**

| Camera arc angle (°) | FOV center displacement from target (mm) | Direction of drift |
|---|---|---|
| 20 | | |
| 30 | | |
| 40 | | |
| 50 | | |
| 55 | | |

### Pass criterion

There is no universal numerical pass/fail for this procedure — it is primarily a **characterization**. However:

- A drift that grows systematically with angle (rather than randomly) indicates a genuine axis-offset error that will introduce a systematic measurement error during skin measurements. If the drift at any position exceeds **~1 mm at the sample plane**, the mechanical cause should be investigated (bearing stack tolerance, arc eccentricity) before proceeding to photometric calibration.
- Drifts < 0.5 mm across the full sweep range are acceptable for most measurement applications.

**Record the maximum drift observed for each arc and its angular location.**

---

## Procedure 5 — Sample Spot Size and FOV Characterization

**Goal:** For every LED position and every camera slit position within the operating range, measure the diameter of the illuminated spot and the diameter of the camera's field of view at the sample plane. These values are needed for measurement-protocol design (Chapter 7) because both change with angle due to the cosine-projection effect.

**Prerequisite:** All previous procedures complete. Camera open, live preview running.

### Setup

1. Place the **flat calibration coupon** (matte white card or diffuse reflectance standard) at the sample aperture, horizontal and flush with the measurement plane.

2. Place the **scale reference** (ruler or printed grid with known line spacing) in the same plane as the coupon surface, within the camera's field of view. The scale must be visible in captured images at all camera arc positions you will measure.

3. In GRControl, select **TIFF** as the image format (for maximum fidelity) and set the output folder to a dedicated subfolder, e.g., `./captures/spot_characterization/`.

### Part A — Illuminated spot diameter vs. LED arc angle

For each LED arc position in {0°, 10°, 20°, 30°, 40°, 50°, 60°}:

4. Command the LED arc (Motor 2) to the target angle with precise positioning enabled.

5. Fix the camera arc at a suitable observation angle (e.g., 45° or the closest slit position to the LED position that avoids occlusion). Keep this camera angle fixed for all LED arc measurements in this part.

6. Turn on the LED being characterized (one LED at a time). Use a consistent brightness across all measurements (e.g., 200).

7. Click **💾 Save** in the Camera Control card to capture a still image. The image is saved as a 16-bit TIFF raw + 8-bit PNG companion. **Use the PNG companion for spot-size measurement** (it is the debayered, viewable version).

8. In an image-analysis application (ImageJ, Python/OpenCV, or similar):
   a. Open the PNG companion image.
   b. Using the scale reference visible in the image, establish a pixel-per-mm conversion factor.
   c. Measure the diameter of the illuminated spot at half-maximum intensity (or at the visual edge of the illuminated region on the matte coupon). Record the diameter in mm.

9. Record: `LED arc angle (°)` | `active LED index` | `camera angle (°)` | `spot diameter (mm)`.

10. Repeat for all seven LED indices (L1–L7) at each LED arc angle.

### Part B — Camera FOV diameter vs. camera arc angle

For each camera arc position in {20°, 25°, 30°, 35°, 40°, 45°, 50°, 55°}:

11. Command the camera arc (Motor 1) to the target angle with precise positioning enabled.

12. Fix the LED arc at 0° (or a consistent reference position). Turn on one LED to illuminate the coupon.

13. Capture a still image using **💾 Save**.

14. In the image-analysis application:
    a. Using the scale reference, determine the pixel-per-mm conversion.
    b. Measure the diameter of the camera's visible field of view at the sample plane. This is the distance across the illuminated/visible region that the camera's sensor covers at that angle and working distance.
    c. Record: `camera arc angle (°)` | `FOV diameter (mm)`.

### Data tables

**Part A — Illuminated spot diameter:**

| LED arc angle (°) | L1 diameter (mm) | L2 (mm) | L3 (mm) | L4 (mm) | L5 (mm) | L6 (mm) | L7 (mm) |
|---|---|---|---|---|---|---|---|
| 0 | | | | | | | |
| 10 | | | | | | | |
| 20 | | | | | | | |
| 30 | | | | | | | |
| 40 | | | | | | | |
| 50 | | | | | | | |
| 60 | | | | | | | |

**Part B — Camera FOV diameter:**

| Camera arc angle (°) | FOV diameter at sample plane (mm) |
|---|---|
| 20 | |
| 25 | |
| 30 | |
| 35 | |
| 40 | |
| 45 | |
| 50 | |
| 55 | |

### Notes on the cosine projection effect

The spot diameter will increase as the LED arc angle increases (farther from normal), because the beam cross-section projected onto the tilted sample plane expands by a factor of `1/cos(θ)`. The ratio of maximum-to-minimum spot size across the range is approximately `1/cos(60°) = 2×` — the spot at 60° is roughly twice as wide as at 0°. Documenting this is the goal; it is not a pass/fail criterion.

---

## Calibration Completion Checklist

Before signing off the geometric calibration:

- [ ] Encoder jitter values recorded (Procedure 1)
- [ ] Camera arc home offset stored in ESP32, repeatability ≤ 0.10° (Procedure 2)
- [ ] LED arc home offset stored in ESP32, repeatability ≤ 0.15° (Procedure 2)
- [ ] LED arc deviation table complete, max deviation ≤ 0.10° (Procedure 3a)
- [ ] Camera arc deviation table complete, max deviation ≤ 0.10° (Procedure 3b)
- [ ] Any systematic deviation pattern documented as known bias (Procedures 3a/3b)
- [ ] Shared-axis drift values recorded for both arcs (Procedure 4)
- [ ] Maximum axis drift < 1 mm (or investigated if larger) (Procedure 4)
- [ ] Spot size table complete for all LED positions × all LED indices (Procedure 5)
- [ ] Camera FOV diameter table complete for all camera positions (Procedure 5)
- [ ] All raw data and images saved to a labelled archive folder

Once all boxes are checked, the instrument has a complete geometric calibration record and is ready to proceed to **photometric calibration (Section 5.2)**.

---

## Troubleshooting Quick Reference

| Symptom | Likely cause | Action |
|---|---|---|
| OME85 encoder shows "—" or erratic large jumps | TXS0108E signal integrity issue (known issue) | Majority-vote is active in firmware; position is reliable if it tracks smoothly. If truly erratic, check wiring and MAX490/MAX3490 supply. |
| Encoder reads constant value during arc sweep | Encoder magnet loose or not tracking | Stop. Inspect encoder magnet/ring-scale mounting. |
| Precise positioning does not converge (residual > 0.1°) | Backlash or wrong-direction approach | Check `move_precise` gain/tolerance settings; jog manually to inspect backlash. |
| LED spot not visible on calibration coupon | LED or PCA9685 wiring issue | Test LEDs individually via the LED Control card. Check INVRT mode (MODE2=0x14). |
| MJPEG preview freezes during arc sweep | MJPEG stream lock conflict | Stop live preview; use single-frame **↻ Refresh** during sweeps instead. |
| Deviation grows monotonically with angle | Belt-thickness gear-ratio error (§8.5) | Document as known bias. Do not re-home. Plan software correction if needed. |
