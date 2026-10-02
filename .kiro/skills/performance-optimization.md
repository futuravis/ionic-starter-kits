---
inclusion: manual
---

# Skill: Performance Optimization

Analyze and optimize the Ionic Angular application's performance.

## Bundle Analysis

### Check Bundle Size
```bash
# Build with stats
npx ng build --stats-json

# Analyze (install source-map-explorer if needed)
npx source-map-explorer www/*.js
```

### Budget Thresholds (from angular.json)
- Initial bundle: warning at 2MB, error at 5MB
- Component styles: warning at 2KB, error at 4KB

## Optimization Checklist

### 1. Lazy Loading
- Every page module should be lazy-loaded via `loadChildren`.
- Check `tabs-routing.module.ts` — all routes should use dynamic imports.
- Heavy third-party libraries should be dynamically imported where possible.

### 2. Change Detection
- Presentational (stateless) components should use `ChangeDetectionStrategy.OnPush`.
- Use `trackBy` with all `*ngFor` directives to prevent unnecessary DOM re-creation.
- Avoid calling functions in templates — use pipes or pre-computed properties.

### 3. Image Optimization
- Use `<custom-img>` (wraps `<ion-img>`) for lazy-loaded images.
- Provide `width` and `height` to prevent layout shifts.
- Use WebP format where possible.
- Consider using responsive image srcsets for different screen densities.

### 4. Virtual Scrolling
- For lists with 50+ items, use `<ion-virtual-scroll>` or `<cdk-virtual-scroll-viewport>`.
- Combine with `<custom-infinite-scroll>` for paginated data.

### 5. Preloading Strategy
- App uses `PreloadAllModules` — this is fine for small apps.
- For larger apps, consider custom preloading strategy that prioritizes visible routes.

### 6. Service Worker / PWA
- Consider adding `@angular/pwa` for offline support and caching.
- Cache static assets and API responses with proper strategies.

### 7. CSS Performance
- Avoid deep selectors (e.g., `::ng-deep` or nested selectors beyond 3 levels).
- Minimize CSS specificity wars — use component scoping.
- Remove unused CSS classes.

## Metrics to Track
- **FCP** (First Contentful Paint): target < 1.5s
- **LCP** (Largest Contentful Paint): target < 2.5s
- **TTI** (Time to Interactive): target < 3.5s
- **CLS** (Cumulative Layout Shift): target < 0.1

## Commands
```bash
# Lighthouse audit (requires Chrome)
npx lighthouse http://localhost:4200 --output=html --output-path=./lighthouse-report.html

# Build production and check sizes
npm run build
ls -la www/*.js | awk '{print $5/1024 "KB", $9}'
```
