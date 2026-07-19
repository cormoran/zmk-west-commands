# zmk-west-commands documentation site

The public documentation for `zmk-west-commands`, built with
[Astro](https://astro.build/) + [Starlight](https://starlight.astro.build/) and
published to GitHub Pages at
<https://cormoran.github.io/zmk-west-commands/>.

The site is bilingual: English lives at the site root and Japanese under `ja/`.

## Local development

```bash
cd website
npm install
npm run dev      # http://localhost:4321/zmk-west-commands/
```

| Command | Action |
| --- | --- |
| `npm run dev` | Start the dev server with hot reload |
| `npm run build` | Build the static site into `website/dist/` |
| `npm run preview` | Preview the built site locally |

## Content layout

```
website/src/content/docs/
  index.mdx              # English home (splash)
  getting-started.mdx
  concepts.mdx
  guides/*.mdx           # one page per west command + CI
  ja/                    # Japanese mirror of every page
```

Add or edit a page by creating/editing the `.mdx` file and, if it should appear
in the sidebar, adding its slug to the `sidebar` array in `astro.config.mjs`.
Every English page under `guides/` should have a Japanese counterpart at the
same path under `ja/`.

## Deployment

Pushes to `main` that touch `website/**` trigger
[`.github/workflows/docs.yml`](../.github/workflows/docs.yml), which builds the
site and deploys it to GitHub Pages. Enable **Settings → Pages → Build and
deployment → Source: GitHub Actions** once for the repository.

The production `site`/`base` default to `https://cormoran.github.io` and
`/zmk-west-commands`; override with the `SITE` / `BASE` environment variables if
you serve the site from a different origin.
