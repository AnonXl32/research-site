# Eitan Elbaz · Security research

Source for **https://research.elbaz.ch**, published from `AnonXl32/research-site` with Jekyll and GitHub Pages.

## Develop and validate

Use Ruby 3.3, Bundler, and Python 3. Windows users should use WSL or the RubyInstaller development kit for native gems.

```sh
bundle install
python3 scripts/verify_article.py
JEKYLL_ENV=production bundle exec jekyll build
python3 scripts/check_site.py _site
bundle exec jekyll serve --host 127.0.0.1
```

The preview runs at `http://127.0.0.1:4000`. `url` always points to the production canonical address. The site uses local assets and system fonts, with no analytics or third-party JavaScript.

## Add an article

Add a publication-ready Markdown file to `_research/`:

```yaml
---
title: Your article title
description: A concise description for listings and sharing.
date: 2026-10-20
category: Reverse engineering
tags: [Research]
---

* On this page
{:toc}

## Introduction

Article text goes here.
```

Use fenced code blocks with lower-case language identifiers such as `cpp`, `llvm`, `rust`, and `nasm`. Put local images in `assets/images/` and include meaningful alt text. Research Markdown is rendered without Liquid evaluation, so braces in code are preserved.

The filename determines the URL (`/research/your-filename/`). Keep it stable after publication. For the VMProtect article, maintain explicit heading IDs because internal links and shared fragments depend on them.

The original VMProtect code examples are checked against `scripts/article-integrity.json`; the supplied hypervisor article is checked against `scripts/hypervisor-integrity.json`. If a technical correction is necessary, document the correction and deliberately update the relevant integrity record. Do not casually regenerate the records to silence a failure.

## Publish a disclosure

Create a Markdown file in `_disclosures/` with `title`, `description`, and `date`. Optional metadata: `identifier`, `severity`, `status`, `affected_versions`, and `fixed_versions`.

Include sections for Summary, Impact, Affected versions, Technical details, Remediation, Disclosure timeline, and References. Publish actual findings and verified identifiers only. Omit private correspondence, credentials, customer information, and embargoed details.

## Publish a document

Add a Markdown file to `_documents/` with `title`, `description`, `date`, and `document_type` (`Designation letter` or `Assessment summary`). The page body provides the public context and summary.

For an existing downloadable file, add `download: /assets/documents/your-file.pdf` and `download_label: PDF`, then place the final redacted file at that path. Remove hidden text, comments, metadata, attachments, and identifying details before committing it. Only publish documents you have selected for public release.

## Publishing boundaries

This repository is public. Keep drafts and confidential work in a separate private location **outside this repository**. `.gitignore` and site exclusions provide additional protection but cannot make a committed file private. The build deliberately excludes source scripts, repository documentation, caches, credentials, and draft directories from the published website.

The Disclosures and Documents indexes show empty states until real entries are added. Contact links point only to the GitHub profile. The site does not accept confidential submissions or provide private document delivery.

## Deploy and maintain

Open a pull request to validate changes and download the `site-preview` build artifact. Merging to `main` automatically builds, checks, and deploys through the `github-pages` environment. The production site updates only after all checks pass.

Update dependencies intentionally with `bundle update`, commit `Gemfile.lock`, and verify the build. Configure GitHub Actions notifications for failed runs in your own GitHub settings if desired.

To undo a published change, use `git revert <commit>` and push the resulting commit to `main`; the workflow redeploys the previous content. Revert the reverting commit to restore the change later.

For account verification, Cloudflare DNS, Pages settings, and HTTPS, see [DEPLOYMENT.md](DEPLOYMENT.md).
