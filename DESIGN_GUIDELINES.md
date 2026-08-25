# Transcriptor Maximus Design Guidelines

This document is the visual and interaction contract for the Transcriptor Maximus client. New UI must extend this system rather than introduce a second style.

## Design thesis

Transcriptor Maximus is a compact field instrument for preserving human speech. Its interface should feel like a consecrated machine console: severe, reliable, tactile, and built to keep working in a dim operations room.

The influence is techno-religious industrial design—not costume decoration. Machinery is treated with ritual care. Important actions feel deliberate. Status is communicated like instrumentation. Ornament must reinforce structure, hierarchy, or state.

The interface should be recognizable through these traits even when no cog, hazard stripe, or franchise reference is visible:

- dense but calm control surfaces;
- squared metal plates and inset bays;
- warm bone text over blackened iron;
- arterial red for recording, danger, and irreversible action;
- aged brass for control and attention;
- cold cyan only for verified healthy state;
- precise labels, restrained motion, and no ornamental softness.

## Core principles

1. **Instrument, not dashboard.** Show the current operation, its state, and the next useful action. Remove explanatory copy when the control already communicates its purpose.
2. **Ritual through sequence.** Checks, recording, sealing, upload, and processing are explicit state transitions. Motion and copy should acknowledge those transitions without theatrical delay.
3. **Structure is ornament.** Borders, rails, dividers, inset panels, hazard marks, and alignment create the character. Do not add decoration that has no structural role.
4. **Evidence over reassurance.** Cyan means a check actually passed. Red means recording, failure, or danger. Never use a success color for an assumed or unchecked state.
5. **Compact, never cramped.** The expanded desktop client is a 440×720 instrument. Primary routes should fit that frame in their normal state; long results and diagnostic failures may scroll.
6. **One visual language.** Reuse existing tokens, panel treatments, icon stroke, and control geometry. A locally attractive component is wrong if it looks imported from another product.

## Palette

Use semantic tokens rather than introducing nearby one-off colors.

| Token | Value | Role |
|---|---:|---|
| `--void` | `#0b0908` | Deepest recesses and window-negative space |
| `--iron` | `#171311` | Primary chassis surface |
| `--iron-raised` | `#201a17` | Raised plates and secondary surfaces |
| `--bone` | `#e9dfcf` | Primary text and high-value labels |
| `--ash` | `#9e9183` | Secondary text and inactive instrumentation |
| `--red` | `#d52d24` | Active recording, failure, danger, destructive focus |
| `--red-dark` | `#6f1715` | Deep red structure, scrollbar, and subdued warning surfaces |
| `--brass` | `#d7a747` | Controls, selected navigation, attention, ritual accents |
| `--cyan` | `#70d7d0` | Confirmed connection, ready state, keyboard focus |
| `--line` | `rgba(231, 214, 190, 0.14)` | Quiet seams between machine plates |

### Color discipline

- Cyan is scarce. Reserve it for confirmed readiness, successful connectivity, and focus visibility.
- Red is not a general accent. Use it for recording, errors, destructive controls, and the primary hazard motif.
- Brass identifies controls and selected structure. It may guide attention but must not compete with an active recording state.
- Bone is the readable foreground; ash carries secondary information. Avoid pure white.
- Surfaces stay warm-black or iron-brown. Do not introduce blue-black SaaS backgrounds.
- Glows are state indicators, not decoration. Keep them tight and local to lamps or active recording marks.

## Geometry and layout

- Default panels are rectangular with `0–4px` corner radii. Larger radii are reserved for genuinely circular mechanisms such as lamps, recording marks, or the collapsed cog.
- Build composition from plates, rails, seams, and bays. Prefer a border and subtle surface shift over a floating card shadow.
- Keep edges aligned across neighboring controls. Inputs and selects in one tool row must share exact top and bottom edges.
- Use compact spacing increments: `4`, `6`, `8`, `10`, `12`, `16`, `18`, and `24px`. Repeated groups should reveal a consistent rhythm.
- The left rail is navigation machinery, not a sidebar card. Active state uses brass text, a restrained red inset marker, and an iron surface shift.
- Preserve the 440×720 expanded frame and 76×76 collapsed mark. Do not solve overflow by enlarging the window.
- Normal task states should fit without scroll where practical. Scroll belongs to variable-length archives, artifacts, and diagnostic messages.

## Typography

- Use the system sans stack for operational text: `Inter`, `ui-sans-serif`, system UI fallbacks.
- Use a system monospace stack for elapsed time, identifiers, byte counts, and machine-readable values.
- Page titles are plain-language and visually dominant. Do not add a paragraph below every title.
- Uppercase micro-labels are permitted only for terse equipment labels such as `SERVER` or artifact eyebrows. They use restrained tracking and never replace readable titles.
- Sentence case is the default for buttons, fields, errors, and navigation.
- Avoid faux-Latin, archaic spelling, or lore-heavy copy. The visual system carries the ritual tone; product language stays direct.

## Components

### Panels

- One-pixel `--line` border over an iron surface.
- At most a slight two-tone gradient to distinguish raised metal from a recess.
- No floating white cards, glassmorphism, frosted blur, or large ambient shadow.
- A colored left edge may communicate a warning or active process.

### Inputs and selects

- Minimum interactive height: `42px` unless the fixed window requires a documented compact exception.
- Dark inset background, bone value text, ash placeholder text.
- Brass border on focus; cyan outer focus ring remains visible for keyboard users.
- Adjacent controls must share geometry. Normalize native search/select appearance when platform chrome breaks alignment.
- Labels name the value. Placeholder text provides an example, not a second label.

### Buttons

- The primary recording action may use a solid red mechanical treatment because it changes system state.
- Secondary actions use iron/brass outlines and quiet fills.
- Destructive or stop actions use red deliberately; routine actions do not.
- Buttons state the result: `Start recording`, `Stop recording`, `Test and save connection`.
- Disabled state remains legible and visibly inert.

### Status

- Every status consists of concise text plus, where useful, one small lamp.
- Grey/ash: unknown, inactive, or not configured.
- Brass: checking, waiting, partial attention.
- Cyan: verified healthy or ready.
- Red: failure, unavailable, or actively recording when the label makes that meaning unambiguous.
- Do not repeat the same state in a badge, table, subtitle, and footer. One overview plus detail on expansion is enough.

### Icons and sigils

- Use the shared stroked SVG system. Icons sit in explicit square containers with grid centering and zero line-height.
- Cogs, the split skull/circuit sigil, archive boxes, waveform bars, antennae, and hazard marks are signature vocabulary.
- Icons support recognition; accessible labels carry meaning. Never use a Unicode glyph as a production icon.
- The primary sigil may be circular. Ordinary action icons and their containers remain square or nearly square.

## Texture and light

- Acceptable: faint scan lines, low-opacity radial warmth, inset seams, subtle metal gradients, a narrow hazard stripe.
- Texture opacity should be low enough that text remains dominant.
- Use a single signature motif per region. A hazard divider plus a cog is enough; adding rivets, chains, seals, and animated noise would become costume.
- Shadows indicate physical elevation only. Avoid generic card shadows and broad neon bloom.

## Motion

- Motion explains machinery: a recording meter moves while capture is active; a cog may rotate slightly on hover; status transitions may tighten or brighten.
- Keep control feedback around `120–180ms`.
- Do not animate idle decoration continuously.
- Respect `prefers-reduced-motion`; all nonessential animation must collapse to effectively zero duration.
- Never delay a real operation to perform a ceremonial animation.

## Content and voice

- Use direct operational language: `Checking server`, `Recording queued`, `No microphones found`.
- State what failed and the next corrective action. Avoid `Something went wrong`.
- Do not apologize, congratulate, or anthropomorphize the machine.
- Avoid redundant subtitles. Add supporting copy only when it changes a decision or prevents an error.
- The product may sound austere, but never obscure. Clarity outranks theme.

## Accessibility

- Every icon-only control requires an accessible name and visible tooltip where helpful.
- Keyboard focus uses the cyan focus ring and must not be removed.
- Never encode status through color alone; pair the lamp with text or an accessible name.
- Maintain readable contrast over textured surfaces.
- Respect reduced-motion preferences.
- Keep targets large enough for pointer use inside the compact window.
- Truncated values must remain discoverable through a title, expanded view, or adjacent detail.

## Anti-patterns

Do not introduce:

- rounded pill layouts as the default component language;
- pastel gradients, purple/blue startup-brand palettes, or bright white cards;
- glassmorphism, blur-heavy overlays, or soft neumorphism;
- generic analytics tiles and decorative statistic cards;
- oversized hero copy inside the utility client;
- unexplained lore, faux scripture, or ornamental Latin;
- gratuitous glow, particle effects, animated noise, or spinning gears;
- duplicate status tables when one state summary and an expanded detail are sufficient;
- page descriptions that merely repeat the title and controls;
- one-off colors, radii, shadows, or icon styles outside the shared system.

## Review checklist

Before merging client UI work, verify:

- The normal route state fits the 440×720 window or has an explicit reason to scroll.
- The page title and controls explain the task without redundant prose.
- Adjacent controls align exactly.
- Status color matches its semantic role and is also expressed in text.
- New colors come from the canonical palette.
- Panels read as connected machine surfaces, not floating web cards.
- Icons use the shared SVG system and have accessible names.
- Focus is visible and reduced motion is respected.
- The change uses one strong motif and removes decorative excess.
- The result still looks like Transcriptor Maximus when all franchise names are removed.
