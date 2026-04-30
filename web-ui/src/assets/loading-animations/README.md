# Pattern Loading Animation

- Asset: `pattern-loading-animation-source.svg`
- Canonical reference: when we say `图案加载动画`, we mean this SVG-based asset
- Meaning: the default "图案加载动画" source for future UI work
- Use only the center animation
- Do not include the surrounding icon frame
- Start from the blank opening state, not from a frame where the animation is already in progress
- Transparent-ready player: `src/app/components/PatternLoadingAnimation.tsx`
- Runtime now uses the provided animated SVG structure instead of video/canvas processing
- The outer white icon tile and border are intentionally excluded in the runtime component

If this asset is later converted to GIF, APNG, sprite frames, or cropped video, preserve the same rules above.
