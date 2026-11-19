# Project Goal

## Vision

Develop an AI photography assistant that combines automatic photo editing with structured photography learning.

The product should help users understand what can be improved in their photos, choose an editing direction, preview the result, export usable Lightroom parameters, and learn from analyzed reference images across different photographic styles.

## Core Modules

### 1. Automatic Photo Editing

Users upload an image. The system analyzes the image and returns:

- Image diagnosis: exposure, contrast, white balance, color cast, subject clarity, composition signals, and style potential.
- Style recommendations: several editing directions such as natural documentary, cinematic, high-key portrait, moody street, film-inspired, clean commercial, or black-and-white.
- User-selectable controls: intensity, warmth, contrast, saturation, color grading, sharpening, noise reduction, crop or composition suggestions.
- Preview image: a generated or locally processed preview that approximates the selected style.
- Lightroom export: a configuration file that can be imported into Lightroom, preferably modern `.xmp` preset output, with legacy `.lrtemplate` considered only if needed.

### 2. Photography Learning

The system collects photography references from permitted online sources and analyzes them by style, theme, and visual structure.

The learning module should provide:

- Reference discovery: gather representative images for selected styles or topics.
- Composition analysis: subject placement, leading lines, symmetry, negative space, depth, framing, foreground and background separation.
- Color analysis: dominant palette, contrast, hue relationships, saturation strategy, tonal distribution, and mood.
- Pattern summaries: recurring techniques and practical takeaways.
- Learning notes: concise guidance that users can apply when shooting or editing their own photos.

## Target Users

- Beginner and intermediate photographers who want concrete editing guidance.
- Lightroom users who want AI-generated presets they can inspect and adjust.
- Visual learners who want to understand why reference photos work.
- Content creators who need consistent style direction across photo sets.

## Success Criteria

- A user can upload a photo, receive multiple style options, preview one option, and export a Lightroom-compatible preset.
- Editing suggestions include a clear rationale, not only parameter values.
- The learning module can produce a structured analysis for a style or topic using properly sourced references.
- The system separates source collection, visual analysis, lesson generation, and user-facing presentation.
- The product remains extensible for future model providers and editing engines.

## Non-Goals For The First Version

- Full Lightroom catalog synchronization.
- Professional retouching operations such as skin frequency separation or object removal.
- Training a custom image model from scratch.
- Unrestricted scraping of copyrighted images.
- Replacing manual creative judgment with a single automatic filter.
