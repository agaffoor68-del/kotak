# Plan: Fix Vercel SSO blocking manifest.webmanifest

## Problem
Vercel Deployment Protection redirects manifest.webmanifest to SSO page causing CORS error. Browser can't fetch manifest when protected.

## Root Cause
Deployment Protection enabled on Vercel project blocks all requests including static assets like manifest.webmanifest. The SSO redirect lacks CORS headers.

## Solution Options
1. **Disable Deployment Protection** (recommended for production) - Vercel Project Settings > Deployment Protection > disable
2. **Configure middleware to bypass protection** - ensure manifest not protected (we added)
3. **Add credentials/crossorigin** - we added
4. **Serve manifest differently** or use vercel.json headers

## Implementation Steps
1. Check if middleware is properly excluding manifest (done)
2. Add vercel.json with headers for manifest
3. Ensure manifest is served as static asset without protection
4. Document that Deployment Protection must be disabled or configured

## Files to modify
- web/vercel.json (create) - add headers for manifest.webmanifest
- web/middleware.ts (already updated)

## Notes
If Vercel Authentication is enabled at project level, middleware can't bypass it. Must disable in Vercel dashboard.
