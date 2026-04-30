import patternLoadingAnimationSource from "../../assets/loading-animations/pattern-loading-animation-source.svg";

export const PATTERN_LOADING_ANIMATION = {
  id: "pattern-loading-animation",
  label: "图案加载动画",
  source: patternLoadingAnimationSource,
  sourceFileName: "pattern-loading-animation-source.svg",
  usageIntent: "Reusable loading animation asset for future UI placements, using the provided animated SVG as the source of truth.",
  runtimeVariant: {
    type: "native-vector",
    componentName: "PatternLoadingAnimation",
    transparent: true,
    sourceRole: "source-of-truth",
  },
  playback: {
    loop: true,
    lightweight: true,
    gpuFriendly: true,
  },
  composition: {
    useOnlyCenterAnimation: true,
    excludeOuterIconFrame: true,
    startFromBlankFrame: true,
  },
  editorNotes: [
    "When the project refers to '图案加载动画', it means this SVG-based animation asset.",
    "Use only the middle animation when embedding this asset into the UI.",
    "Do not include the surrounding icon frame or outer badge.",
    "Start playback from the blank opening state, before the inner animation has progressed.",
    "Prefer the provided animated SVG structure as the source of truth in runtime instead of video or per-frame canvas processing.",
  ],
} as const;

export function getPatternLoadingAnimation() {
  return PATTERN_LOADING_ANIMATION;
}
