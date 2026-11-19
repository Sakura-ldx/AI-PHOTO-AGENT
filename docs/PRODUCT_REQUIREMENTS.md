# Product Requirements

## MVP Scope

The first usable version should validate the complete workflow with limited depth:

1. Upload one image.
2. Analyze the image with a vision model.
3. Return 3 editing style recommendations.
4. Let the user choose one style and adjust intensity.
5. Generate a preview image.
6. Export Lightroom-compatible `.xmp` preset parameters.
7. Search or import a small set of reference images for one learning topic.
8. Analyze composition and color for those references.
9. Generate a concise learning summary.

## Automatic Photo Editing Requirements

### Image Input

- Support common image formats: JPEG and PNG for MVP.
- Preserve the original image and generate derived previews separately.
- Store metadata needed for analysis, including dimensions, color mode, and file type.

### Analysis Output

The analysis result should include:

- Overall image summary.
- Technical diagnosis.
- Composition observations.
- Color and tone observations.
- Recommended styles.
- Parameter rationale for each style.

### Style Recommendation

Each recommended style should include:

- Name.
- Short description.
- Best-fit reason.
- Editable parameter set.
- Expected visual impact.

### Preview

The preview does not need to perfectly match Lightroom in the MVP, but it should approximate:

- Exposure.
- Contrast.
- White balance.
- Highlights and shadows.
- Saturation and vibrance.
- Basic color grading.
- Sharpening or clarity where practical.

### Lightroom Export

The export system should generate a preset file with:

- A stable preset name.
- Supported development parameters.
- User-selected style intensity reflected in values.
- Versioned mapping from internal parameters to Lightroom-compatible fields.

## Photography Learning Requirements

### Source Collection

- Use permitted image sources, official APIs, curated datasets, or user-provided reference images.
- Store source URL, author, license, and retrieval time when available.
- Avoid collecting images from sources that forbid automated access.

### Visual Analysis

Each reference image analysis should include:

- Composition structure.
- Subject placement.
- Visual hierarchy.
- Color palette.
- Tonal distribution.
- Mood and style labels.
- Practical lessons.

### Learning Summary

The generated learning output should include:

- Style overview.
- Common composition patterns.
- Common color strategies.
- Mistakes to avoid.
- Exercises the user can try.

## Data Objects

### Uploaded Photo

- `id`
- `original_file`
- `preview_files`
- `metadata`
- `analysis`
- `created_at`

### Editing Style

- `id`
- `name`
- `description`
- `parameters`
- `rationale`
- `preview_id`

### Lightroom Preset

- `id`
- `style_id`
- `format`
- `parameter_mapping_version`
- `file_path`
- `created_at`

### Learning Reference

- `id`
- `source_url`
- `author`
- `license`
- `image_file`
- `analysis`
- `created_at`

## Open Product Questions

- Should the MVP focus on web upload first, or a local desktop workflow for Lightroom users?
- Which model provider should be used for image understanding and editing previews?
- Should reference discovery rely on public APIs, curated datasets, or user-supplied images first?
- How exact must the preview be compared with Lightroom rendering for the first release?
