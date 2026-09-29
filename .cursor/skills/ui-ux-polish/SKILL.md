---
name: ui-ux-polish
description: Use whenever building, restyling, or reviewing web UI (pages, landing pages, dashboards, components, forms, layouts, CSS/Tailwind). Produces distinctive, consistent, accessible, responsive interfaces with clear visual hierarchy, complete interaction states, and polished details, instead of generic template-looking output. Also use when the user says the UI looks ugly, plain, boring, or "AI-generated".
---

# UI/UX Polish

Goal: every interface you write should look intentionally designed by a human designer, be pleasant to use, and be consistent with the existing codebase.

Reply to the user in the language they write in (code and identifiers stay in English).

## Workflow (follow in order)

### 1. Inspect before designing
- Read the existing code first: framework, styling approach (Tailwind, CSS modules, styled-components), component library (shadcn, MUI, Radix...), existing tokens, fonts, icons.
- Reuse what exists. Extend the existing design system rather than inventing a parallel one.
- If it is a brand-new project, you are free to define the system (steps 2-3).
- Identify the audience and purpose of the screen: who uses it, what is the ONE primary action.

### 2. Commit to a design direction
Pick one clear direction and state it in one sentence before coding (e.g. "dark cinematic editorial, serif headings, warm gold accent"). See `references/design-direction.md`.
- Consistency of one bold idea beats a mix of many safe ones.
- Match the tone to the content (history/documentary ≠ fintech dashboard ≠ kids app).

### 3. Define tokens first
Never scatter raw hex values, magic pixel numbers, or ad-hoc fonts. Create or reuse design tokens for color, typography, spacing, radius, shadow, motion. See `references/tokens.md`.

### 4. Build hierarchy and layout
- One primary focal point per screen/section. Size, weight, color and whitespace establish the order of importance.
- Use a grid, a consistent container width, and a vertical rhythm between sections. See `references/layout-responsive.md`.
- Mobile-first. Verify at 375px, 768px, 1440px.

### 5. Build components with ALL states
Every interactive element needs: default, hover, focus-visible, active, disabled, loading. Every data view needs: loading (skeleton), empty, error, success. See `references/components.md`.

### 6. Polish
- Micro-interactions: transitions 150-250ms, ease-out, animate only `transform` and `opacity`.
- Depth: subtle layered shadows, borders, or surface color steps, not heavy drop shadows.
- Imagery: real aspect ratios, `object-fit`, lazy loading, meaningful `alt`, overlays/gradients only to protect text contrast.
- Respect `prefers-reduced-motion` and `prefers-color-scheme`.

### 7. Verify before finishing
Run through `references/review-checklist.md`. Fix issues, do not just list them. If you can open the app in a browser, look at it at mobile and desktop widths and compare against the checklist.

## Non-negotiables
- Contrast: body text ≥ 4.5:1, large text and UI borders ≥ 3:1.
- Touch targets ≥ 44×44px, visible keyboard focus on everything interactive.
- Text is real text (not baked into images); inputs have visible labels.
- No layout shift when content loads (reserve space for images, fonts, async data).
- Fonts must support the content's language. For Vietnamese, load the `vietnamese` subset and test diacritics (ằ, ữ, ệ, ố) at all sizes and line-heights. See `references/design-direction.md`.
- Copy is realistic and specific. No lorem ipsum, no "Feature One / Feature Two".

## Avoid the "AI template" look
See `references/anti-patterns.md`. Short version: no default purple-blue gradients, no identical rounded cards in a 3-column grid for everything, no emoji as icons, no centered-everything layouts, no random shadows and colors.

## References (read only what the task needs)
- `references/design-direction.md`: choosing an aesthetic, typography pairings, color strategy
- `references/tokens.md`: token templates (CSS variables + Tailwind mapping), scales
- `references/layout-responsive.md`: grid, spacing rhythm, breakpoints, hero/section patterns
- `references/components.md`: buttons, forms, cards, nav, modals, tables, toasts, states
- `references/accessibility.md`: practical a11y checklist
- `references/anti-patterns.md`: generic-output tells and how to fix them
- `references/review-checklist.md`: final self-review