---
inclusion: manual
---

# Skill: Capacitor Build & Deploy

Build the app for native platforms using Capacitor 8.

## Prerequisites
- Capacitor CLI installed (`@capacitor/cli` in devDependencies)
- Platform added: `npx cap add ios` or `npx cap add android`

## iOS Build
```bash
# 1. Build the web assets
npm run build

# 2. Sync to native project
npx cap sync ios

# 3. Open in Xcode
npx cap open ios
```

## Android Build
```bash
# 1. Build the web assets
npm run build

# 2. Sync to native project
npx cap sync android

# 3. Open in Android Studio
npx cap open android
```

## Live Reload (Development)
```bash
# Start dev server with external host
npx ng serve --host=0.0.0.0 --disable-host-check

# In another terminal, run on device with live reload
npx cap run ios --livereload --external
npx cap run android --livereload --external
```

## Common Tasks
```bash
# Copy web assets only (no plugin sync)
npx cap copy

# Full sync (copy + update plugins + update native configs)
npx cap sync

# Check for Capacitor plugin updates
npx cap doctor
```

## Capacitor Plugins in this Project
- `@capacitor/app` — App lifecycle events
- `@capacitor/haptics` — Haptic feedback
- `@capacitor/keyboard` — Keyboard events and control
- `@capacitor/status-bar` — Status bar styling

## Notes
- Build output goes to `www/` (configured in angular.json)
- Capacitor config should be at project root as `capacitor.config.ts`
- If no config exists yet, create one:
  ```typescript
  import type { CapacitorConfig } from '@capacitor/cli';

  const config: CapacitorConfig = {
    appId: 'com.example.ionicstarter',
    appName: 'Ionic Starter',
    webDir: 'www',
    server: {
      androidScheme: 'https',
    },
  };

  export default config;
  ```
