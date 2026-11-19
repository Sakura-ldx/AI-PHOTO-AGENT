# Technical Direction

## Recommended Initial Shape

Start with a web application backed by a small service layer:

- Frontend: upload, analysis result, style selection, preview, preset export, and learning views.
- Backend service: image storage, model calls, parameter conversion, preview rendering, and reference analysis.
- Model adapters: isolated interfaces for vision analysis, text generation, and possible image transformation.
- Export module: deterministic Lightroom preset generation from internal editing parameters.

This keeps the product usable in a browser while preserving the option to later add a desktop helper or Lightroom-focused workflow.

## Suggested Architecture

```text
User Interface
  -> Upload and session management
  -> Editing recommendations
  -> Preview comparison
  -> Lightroom preset export
  -> Photography learning workspace

Application Service
  -> Image metadata extraction
  -> Model orchestration
  -> Editing parameter schema
  -> Preview rendering
  -> Reference image analysis

Adapters
  -> Vision model provider
  -> Image processing engine
  -> Web source or dataset provider
  -> Lightroom preset writer

Storage
  -> Original uploads
  -> Generated previews
  -> Preset exports
  -> Analysis JSON
  -> Reference image metadata
```

## Editing Parameter Schema

Use an internal schema first, then map it to export formats:

- `exposure`
- `contrast`
- `highlights`
- `shadows`
- `whites`
- `blacks`
- `temperature`
- `tint`
- `vibrance`
- `saturation`
- `texture`
- `clarity`
- `dehaze`
- `tone_curve`
- `hsl`
- `color_grading`
- `sharpening`
- `noise_reduction`
- `crop_suggestion`

The internal schema should be versioned so model prompts, preview rendering, and Lightroom export can evolve independently.

## Lightroom Export Strategy

Use `.xmp` as the preferred preset output format for modern Lightroom workflows.

Implementation should avoid hardcoding model responses directly into `.xmp`. Instead:

1. Model returns style rationale and normalized editing intent.
2. Application validates and clamps parameters.
3. Internal schema stores versioned values.
4. Export adapter maps internal fields to Lightroom-compatible XMP fields.

## Preview Strategy

Preview rendering can start as an approximation using a local image processing library. The preview should be labeled internally as an approximation and improved over time.

Priority preview controls:

- Exposure and contrast.
- White balance.
- Highlights and shadows.
- Saturation and vibrance.
- Color grading approximation.

## Photography Learning Source Policy

The learning module should only ingest images from:

- User-provided uploads.
- Sources with explicit API access.
- Public datasets with compatible licenses.
- Curated manually approved image lists.

For every reference, store source metadata when available:

- URL.
- Author.
- License.
- Access method.
- Retrieval timestamp.

## Key Risks

- Lightroom preview mismatch: local previews may not exactly match Lightroom rendering.
- Copyright and source access: unrestricted scraping can create legal and product risk.
- Model inconsistency: style suggestions need validation and bounded parameter ranges.
- Cost and latency: image analysis and preview generation can become expensive at scale.
- User trust: recommendations must explain why a parameter or style was chosen.
