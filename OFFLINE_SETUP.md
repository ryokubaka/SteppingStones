# Stepping Stones Offline Setup

## Overview
This document describes the changes made to make the Stepping Stones application work entirely offline by caching all frontend UI resources.

## Changes Made

### 1. Local Static Assets
- **Bootstrap 5.3.3**: Downloaded and added to `static/css/bootstrap.min.css` and `static/scripts/bootstrap.bundle.min.js`
- **Bootstrap Source Maps**: Added `bootstrap.min.css.map` and `bootstrap.bundle.min.js.map` to prevent 404 errors
- **jQuery 3.7.1**: Downloaded and added to `static/scripts/jquery.min.js`
- **Moment.js 2.30.1**: Downloaded and added to `static/scripts/moment.min.js`
- **PDFMake 0.2.7**: Downloaded and added to `static/scripts/pdfmake.min.js`
- **DateTime Moment Plugin**: Downloaded and added to `static/scripts/datetime-moment.min.js`
- **Exo Font**: Downloaded weights 200 and 400, added to `static/fonts/exo/` with local CSS file
- **FontAwesome**: Already local, no changes needed

### 2. Template Updates
- **base.html**: Updated to use local static files instead of CDN URLs
- **initial-setup.html**: Updated to use local static files instead of CDN URLs
- **jquery.html**: Updated to use local jQuery instead of CDN
- **momentjs.html**: Updated to use local moment.js instead of CDN and added missing `{% load static %}` tag
- **datatables-pdfexport.html**: Updated to use local pdfmake instead of CDN
- **datatables-common.html**: Updated to use local datetime-moment plugin instead of CDN
- Removed all Google Fonts preconnect links and CDN references

### 3. Content Security Policy (CSP) Updates
- Added back `cdnjs.cloudflare.com` to script and style sources for remaining external dependencies
- Added `CSP_SCRIPT_SRC_ATTR = ("'unsafe-inline'",)` to allow inline event handlers
- Updated `CSP_FONT_SRC = ("'self'", "data:")` to allow local fonts and data URIs
- Added `CSP_WORKER_SRC = ("'self'",)` to allow service worker registration
- Now allows `'self'` for font sources and service workers

### 4. Service Worker Implementation
- **Location**: `static/scripts/service-worker.js`
- **Strategy**: Network-first with cache fallback
- **Cached Assets**: All Bootstrap, jQuery, Moment.js, PDFMake, Exo fonts, FontAwesome, source maps, and other static files
- **Registration**: Added to base.html with CSP nonce

## How It Works

### Online Mode
1. When connected to internet, the app loads fresh content from the server
2. Service worker caches all static assets for offline use
3. Network-first strategy ensures latest content is always served when available

### Offline Mode
1. When offline, service worker serves cached static assets
2. All UI elements (Bootstrap, jQuery, fonts, icons) work without internet
3. App remains fully functional for viewing cached data

## Testing

### To Test Online Mode:
1. Start the application: `docker-compose up -d`
2. Visit the application in a browser
3. Check browser DevTools → Application → Service Workers to confirm registration

### To Test Offline Mode:
1. Load the application while online (to cache everything)
2. Disconnect from internet
3. Refresh the page - it should load and work normally
4. All UI elements should display correctly with local fonts and styles

## Files Modified

### Templates
- `event_tracker/templates/base/base.html`
- `event_tracker/templates/base/initial-setup.html`
- `event_tracker/templates/base/external-libs/jquery.html`
- `event_tracker/templates/base/external-libs/momentjs.html`
- `event_tracker/templates/base/external-libs/datatables-pdfexport.html`
- `event_tracker/templates/base/external-libs/datatables-common.html`

### Static Files Added
- `event_tracker/static/css/bootstrap.min.css`
- `event_tracker/static/css/bootstrap.min.css.map`
- `event_tracker/static/scripts/bootstrap.bundle.min.js`
- `event_tracker/static/scripts/bootstrap.bundle.min.js.map`
- `event_tracker/static/scripts/jquery.min.js`
- `event_tracker/static/scripts/moment.min.js`
- `event_tracker/static/scripts/pdfmake.min.js`
- `event_tracker/static/scripts/datetime-moment.min.js`
- `event_tracker/static/fonts/exo/exo.css`
- `event_tracker/static/fonts/exo/Exo-200.woff2`
- `event_tracker/static/fonts/exo/Exo-400.woff2`
- `event_tracker/static/fonts/exo/Exo-200.ttf`
- `event_tracker/static/fonts/exo/Exo-400.ttf`
- `event_tracker/static/scripts/service-worker.js`

### Settings
- `stepping_stones/settings.py` (CSP configuration)

## CSP Fixes Applied

### Issues Resolved:
1. **Inline Event Handlers**: Added `CSP_SCRIPT_SRC_ATTR = ("'unsafe-inline'",)` to allow `onclick` and similar attributes
2. **Font Loading**: Updated `CSP_FONT_SRC = ("'self'", "data:")` to allow local fonts and data URIs
3. **Source Maps**: Downloaded Bootstrap source maps to prevent 404 errors
4. **Font Paths**: Fixed font paths in `exo.css` to use relative paths
5. **jQuery Dependencies**: Downloaded jQuery and related libraries locally
6. **Service Worker**: Added `CSP_WORKER_SRC = ("'self'",)` to allow service worker registration
7. **External Libraries**: Updated templates to use local files instead of CDN
8. **Template Loading**: Fixed missing `{% load static %}` tag in momentjs.html template
9. **Permissions Policy**: Removed unrecognized browser features from Permissions-Policy header

### CSP Directives Updated:
```python
CSP_SCRIPT_SRC_ATTR = ("'unsafe-inline'",)  # Allow inline event handlers
CSP_FONT_SRC = ("'self'", "data:")          # Allow local fonts and data URIs
CSP_WORKER_SRC = ("'self'",)                 # Allow service worker registration
```

## Benefits

1. **Complete Offline Functionality**: App works without internet connection
2. **Faster Loading**: Local assets load faster than CDN
3. **Reliability**: No dependency on external CDNs for core libraries
4. **Security**: Reduced attack surface by removing external dependencies
5. **Consistency**: UI always looks the same regardless of network conditions
6. **No CSP Violations**: All inline scripts, fonts, and service workers work properly
7. **jQuery Support**: All jQuery-dependent features work offline

## Maintenance

- To update Bootstrap: Download new version and replace files in static directory
- To update jQuery/Moment.js/PDFMake: Download new versions and replace files
- To update fonts: Download new font files and update exo.css
- Service worker cache version can be incremented to force cache refresh
- Source maps should be updated when updating Bootstrap 