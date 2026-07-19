// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// GitHub Pages project site: https://cormoran.github.io/zmk-west-commands
// Override with SITE / BASE env vars when serving from a different origin.
const site = process.env.SITE ?? "https://cormoran.github.io";
const base = process.env.BASE ?? "/zmk-west-commands";

export default defineConfig({
  site,
  base,
  trailingSlash: "always",
  integrations: [
    starlight({
      title: "zmk-west-commands",
      tagline: "west commands for building & hardware-free testing of ZMK modules",
      logo: {
        src: "./src/assets/logo.svg",
        replacesTitle: false,
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/cormoran/zmk-west-commands",
        },
      ],
      editLink: {
        baseUrl:
          "https://github.com/cormoran/zmk-west-commands/edit/main/website/",
      },
      defaultLocale: "root",
      locales: {
        root: { label: "English", lang: "en" },
        ja: { label: "日本語", lang: "ja" },
      },
      sidebar: [
        {
          label: "Start Here",
          translations: { ja: "はじめに" },
          items: [
            { slug: "getting-started" },
            { slug: "concepts" },
          ],
        },
        {
          label: "Command Guides",
          translations: { ja: "コマンドガイド" },
          items: [
            { slug: "guides/zmk-build" },
            { slug: "guides/zmk-test" },
            { slug: "guides/zmk-renode-test" },
            { slug: "guides/zmk-ble-test" },
          ],
        },
        {
          label: "CI & Automation",
          translations: { ja: "CI と自動化" },
          items: [{ slug: "guides/github-actions" }],
        },
      ],
    }),
  ],
});
