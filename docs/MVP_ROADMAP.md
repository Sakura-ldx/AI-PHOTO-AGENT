# MVP Roadmap

## Phase 0: Project Foundation

- Define product goal and scope.
- Choose initial application architecture.
- Define internal editing parameter schema.
- Define Lightroom export mapping.
- Define image source policy for learning references.

## Phase 1: Automatic Editing Prototype

- Build image upload flow.
- Extract image metadata.
- Call a vision model for image diagnosis.
- Generate 3 style recommendations.
- Convert selected style into internal editing parameters.
- Render a local preview.
- Export an `.xmp` preset file.

## Phase 2: Photography Learning Prototype

- Add reference image import or permitted source search.
- Analyze composition and color for reference images.
- Group findings by style.
- Generate reusable learning notes.
- Show references, visual analysis, and learning summary in one workflow.

## Phase 3: Product Integration

- Connect editing suggestions with learning examples.
- Let users compare their uploaded photo with analyzed references.
- Save user sessions and generated presets.
- Add account, history, and collection concepts if needed.

## Phase 4: Quality And Expansion

- Improve Lightroom parameter fidelity.
- Add more image formats and RAW-adjacent workflows.
- Add batch editing suggestions.
- Add style libraries.
- Add richer source governance and attribution.

## First Engineering Milestones

1. Create a minimal web interface for upload, analysis result, style selection, preview, and preset export.
2. Implement the internal editing parameter schema.
3. Implement `.xmp` export generation.
4. Add a model adapter interface for image analysis.
5. Add a reference image ingestion path for photography learning.
