---
name: automotive-montage-effects
description: Create original, high-energy automotive montages with reference-aware pacing, speed ramps, restrained liquid-glass distortion, flash accents, cyan car contours, LUT grading, and cut-synced sound.
---

# Automotive Montage Effects

Use this skill when editing user-supplied or licensed car footage into a fast,
detail-led montage. The reference style is energetic automotive-show footage:
vary paint colors and locations, alternate full-car views with wheel, grille,
light, and bodywork details, and use short horizontal whip-blur accents between
selected shots. A dark, original title card can close the piece when requested.
Keep the requested output aspect ratio rather than copying the reference frame
shape.

## Guardrails

- Borrow pacing and broad visual treatment, not the reference's shots, logo,
  exact cut sequence, music, or sound recording.
- Prefer hard cuts and motivated whip transitions. Avoid adding a dissolve or
  effect to every cut.
- Keep displacement and flash accents brief. Do not create repeated strobing.
- A general edge detector outlines the whole image, not just the car. For
  car-only light lines, use a tracked/rotoscoped car mask; if no mask is
  available, keep the full-frame edge accent faint and sparse.
- Preserve source proportions, protect highlights, and inspect plates, badges,
  and other readable details before delivery.

## Workflow

1. Probe the reference and every source with `ffprobe`. Note duration, frame
   rate, dimensions, orientation, and whether source audio is usable.
2. Build a reusable reference profile and pass all future uploads as additional
   `--source` arguments:

   ```powershell
   python -m lib.reference_style path\to\reference.mp4 `
     --sources-dir assets `
     --source path\to\uploaded-footage.mov `
     --output project\reference-style.json
   ```

   Use the detected shot-length distribution and palette as starting points;
   choose the final shot order and pacing for the new footage.
3. Select a visual progression: establish the car, alternate wide and detail
   shots, then reserve the strongest angle or movement for the ending. Reframe
   with crop/pad, never stretch. Match the user's requested aspect ratio.
4. Build effects as separate timeline passes. Preview the ungraded cut first,
   then add speed changes, distortion, contour accents, flashes, grade, and
   sound. This makes artifacts and timing errors easier to isolate.
5. Render a short representative section before the full montage. Review
   frames at every effect onset and cut; reduce or remove any effect that hides
   the car or damages detail.

## Effect recipe

### Speed ramps

Approximate a ramp with adjacent, non-overlapping trimmed segments and distinct
playback speeds.
For a source-time segment of duration `d` played at speed `s`, use
`setpts=(PTS-STARTPTS)/s`; `s < 1` is slow motion and `s > 1` is fast motion.
Join matching-size/pixel-format segments with `concat`, then normalize to the
delivery frame rate and reset PTS. Use frame-based trims at the source frame
rate to avoid including a boundary frame twice. Check the resulting duration
before re-timing the next cut. Use two or three segments for a readable
ease-in/ease-out ramp, not many tiny speed changes. Use optical-flow
interpolation only when the source frame rate supports it and its artifacts
have been checked.

Example pattern:

```text
[0:v]split=2[a][b];
[a]trim=start_frame=0:end_frame=15,setpts=(PTS-STARTPTS)/0.5[a1];
[b]trim=start_frame=15:end_frame=30,setpts=(PTS-STARTPTS)/2[b1];
[a1][b1]concat=n=2:v=1:a=0,fps=30,setpts=N/(30*TB)
```

This example uses 30 fps input and output; replace the frame ranges and both
`30` values with the chosen shot's source and delivery rates.
If retaining production audio, split and trim it to the same source ranges and
apply matching `atempo` factors; otherwise use original designed sound effects.

### Liquid-glass displacement

Use FFmpeg's `displace` with animated grayscale X/Y maps centered near neutral
gray. Keep map variation small (start around 4-8 code values from neutral), use
`edge=smear`, and pulse it for only a few frames around a close-up or transition.
Mask the effect to reflections/bodywork when practical; a full-frame warp can
make the car shape look broken. Check the first and last frame of each pulse for
a visible snap.

### Flash accents

Use a one- or two-frame brightness/color lift at selected cut points, not a
continuous glow. At 30 fps, a `between(t,cut,cut+0.04)` enable window is about
one frame. Keep whites below clipping and leave enough unflashed frames between
accents to avoid strobing.

### Cyan contour lines

Create a restrained edge alpha, tint it cyan, and composite it over the original
with a normal alpha overlay. When the car mask is available, multiply the edge
alpha by that mask before tinting so the lines follow the car rather than
outlining the garage or background. Do not use additive/screen blending as a
shortcut; it can introduce a purple cast. Keep line opacity low enough to retain
body detail.

### LUT grading

Apply exposure and white balance before a creative LUT. The reusable grades in
this repository are:

- `renders/garage-noir-cool.cube` for the cool, contrasty base.
- `renders/liquid-glass-cyan.cube` for restrained cyan accents.

Treat these as starting points: adapt exposure and saturation to each source,
match adjacent shots, and avoid crushing black paint or clipping white paint.

### Cut-synced sound

Use original or properly licensed whooshes, glass ticks, and low-end impacts.
Place transients on selected cuts and ramps; keep ambience/music underneath
instead of stacking a loud hit on every edit. Do not extract or imitate the
reference recording. Mix to the requested delivery format, apply a limiter, and
check the finished audio for clipping and abrupt tails.

## Focused FFmpeg smoke check

This PowerShell example runs the displacement, cyan edge overlay, flash, and
garage LUT on synthetic footage without writing an output file. Run it from the
repository root; adapt quoting for other shells.

```powershell
$fc = "[0:v][1:v][2:v]displace=edge=smear[glass];[glass]split[base][edgeSrc];[edgeSrc]edgedetect=mode=wires:low=0.06:high=0.16,format=rgba,colorkey=black:0.12:0.05,colorchannelmixer=rr=0:gg=0.8:bb=1:aa=0.45[contour];[base][contour]overlay=shortest=1,eq=brightness=0.3:enable='between(t,0.3,0.34)',lut3d=file=renders/garage-noir-cool.cube,format=yuv420p[out]"
ffmpeg -hide_banner -v error `
  -f lavfi -i "testsrc2=size=320x180:rate=30:duration=1" `
  -f lavfi -i "color=gray:s=320x180:r=30:d=1,geq=lum='128+4*sin((X+T*20)/24)':cb=128:cr=128" `
  -f lavfi -i "color=gray:s=320x180:r=30:d=1,geq=lum='128+4*cos((Y+T*20)/24)':cb=128:cr=128" `
  -filter_complex $fc -map "[out]" -f null NUL
```

## Delivery checks

- Confirm the rendered duration, dimensions, frame rate, and audio streams with
  `ffprobe`; keep the requested aspect ratio and codec.
- Decode the entire render with
  `ffmpeg -v error -i output.mp4 -f null NUL`.
- Extract a contact sheet spanning the intro, ramps, transitions, and ending.
  Confirm flashes are brief, cyan lines do not tint the frame purple, and
  displacement has no visible jump at its boundaries.
- Listen to the full mix and measure peaks; fix clipping, unintended source
  audio, or abrupt sound tails before delivery.
