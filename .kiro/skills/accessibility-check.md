---
inclusion: manual
---

# Skill: Accessibility Check

Audit the application for common accessibility issues specific to Ionic Angular apps.

## What to Check

### 1. Color Contrast
- Verify text colors meet WCAG AA (4.5:1 for normal text, 3:1 for large text).
- Check that Ionic color tokens have proper contrast values set.
- Verify the high contrast palette is imported (`high-contrast.system.css`).

### 2. Interactive Elements
- All `<custom-button>` and `<ion-button>` elements should have visible text or `aria-label`.
- Icon-only buttons MUST have `aria-label` or `ariaLabel` attribute.
- Touch targets should be at least 44x44px (already enforced by `--app-button-height: 44px`).

### 3. Form Inputs
- Every `<custom-input>`, `<custom-select>`, `<custom-textarea>` should have a `label` prop or associated `<ion-label>`.
- Error messages should use `aria-live="polite"` for screen reader announcements.
- Required fields should have `aria-required="true"`.

### 4. Navigation
- Page titles should be unique and descriptive (set via `<ion-title>`).
- Tab navigation should be logical (no tabindex > 0).
- Skip links or focus management on route change.

### 5. Images & Media
- All `<custom-img>`, `<img>`, `<ion-img>` must have `alt` attribute.
- Decorative images should have `alt=""`.
- Icons used for meaning (not decoration) need `aria-label`.

### 6. Dynamic Content
- `*ngIf` toggled content should manage focus appropriately.
- Modals/overlays should trap focus and return focus on close.
- Toast notifications should use `aria-live` regions.

### 7. Motion & Animation
- Respect `prefers-reduced-motion` media query.
- Provide option to disable animations.

## Template Checks
```bash
# Find images without alt
grep -rn '<img' src/app/ --include="*.html" | grep -v 'alt='
grep -rn '<custom-img' src/app/ --include="*.html" | grep -v 'alt='

# Find icon buttons without aria-label
grep -rn 'custom-button-clear' src/app/ --include="*.html" | grep -v 'aria'

# Find inputs without labels
grep -rn 'custom-input' src/app/ --include="*.html" | grep -v 'label'
```

## Ionic 8 Accessibility Features
- High contrast palette: Already imported via `high-contrast.system.css`
- Dark mode: System-responsive via `dark.system.css`
- `color-scheme: light dark` meta tag: Already in `index.html`
- Focus ring: Global `:focus-visible` style defined in `global.scss`

## Reference
- WCAG 2.1 AA: https://www.w3.org/WAI/WCAG21/quickref/
- Ionic Accessibility: https://ionicframework.com/docs/reference/glossary#a11y
