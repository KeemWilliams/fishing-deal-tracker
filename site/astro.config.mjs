import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// Public MVP site for the deep-discount fishing gear tracker. Static output,
// currently deployed to GitHub Pages as a PROJECT site (served under the
// `/fishing-deal-tracker/` path, not the domain root) -- hence `base` below.
// Deal data is read from a build-time local export (see src/lib/feed.ts),
// with a small client-side script that re-checks the published feed for
// updates and shows a stale-feed banner if the export goes quiet. See
// README.md for the feed contract and PUBLIC_/FEED_ env vars.
//
// If this ever moves to a custom domain or a user/org Pages site (served at
// the domain root), `base` becomes '/' and `site` changes to match --
// nothing else in the site depends on the base path being non-root, every
// internal link and asset reference goes through import.meta.env.BASE_URL.
export default defineConfig({
  site: 'https://keemwilliams.github.io',
  base: '/fishing-deal-tracker',
  trailingSlash: 'always',
  compressHTML: true,
  vite: {
    plugins: [tailwindcss()],
  },
});
