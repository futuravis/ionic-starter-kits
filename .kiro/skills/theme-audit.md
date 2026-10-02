---
inclusion: manual
---

# Skill: Theme Audit

Scan the project's SCSS files for hardcoded values that should use design tokens, and suggest replacements.

## What to Check

### 1. Hardcoded Colors
Search for hex (`#xxx`, `#xxxxxx`), `rgb()`, `rgba()`, `hsl()`, `hsla()` values in:
- `src/app/**/*.scss`
- Exclude `src/theme/variables.scss` (that's where tokens are defined)
- Exclude `src/global.scss` (managed separately)

**Replace with:** `var(--app-bg)`, `var(--app-surface)`, `var(--app-text)`, `var(--ion-color-primary)`, etc.

### 2. Hardcoded Font Sizes
Search for `font-size: Npx` or `font-size: Nrem` patterns.

**Replace with:** `var(--app-font-size-*)` tokens.

### 3. Hardcoded Font Weights
Search for `font-weight: N` where N is a number.

**Replace with:** `var(--app-font-weight-regular|medium|semibold|bold)`.

### 4. Hardcoded Border Radius
Search for `border-radius: Npx` patterns.

**Replace with:** `var(--app-radius-xs|sm|md|lg|xl|round)`.

### 5. Hardcoded Box Shadows
Search for `box-shadow:` with literal values.

**Replace with:** `var(--app-shadow-sm|md|lg|xl)`.

### 6. Hardcoded Spacing
Search for `padding:` or `margin:` with pixel values not using tokens.

**Replace with:** `var(--app-space-N)` or Ionic utility classes.

## Output Format
For each violation found, report:
```
FILE: src/app/{path}
LINE: {number}
FOUND: {the hardcoded value}
REPLACE WITH: {suggested token}
```

## Commands to Run
```bash
# Find hardcoded colors (excluding theme files)
grep -rn '#[0-9a-fA-F]\{3,8\}' src/app/ --include="*.scss" | grep -v variables.scss | grep -v global.scss

# Find hardcoded px font sizes
grep -rn 'font-size:.*px' src/app/ --include="*.scss"

# Find hardcoded border-radius
grep -rn 'border-radius:.*px' src/app/ --include="*.scss"

# Find hardcoded box-shadow
grep -rn 'box-shadow:' src/app/ --include="*.scss" | grep -v 'var('
```

## Reference
#[[file:src/theme/variables.scss]]
