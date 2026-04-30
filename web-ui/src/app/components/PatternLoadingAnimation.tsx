type PatternLoadingAnimationProps = {
  className?: string;
  ariaLabel?: string;
};

function buildClassName(className?: string) {
  return className ? `pattern-loading-animation-svg ${className}` : "pattern-loading-animation-svg";
}

export function PatternLoadingAnimation({
  className,
  ariaLabel = "图案加载动画",
}: PatternLoadingAnimationProps) {
  return (
    <div
      className={buildClassName(className)}
      role="img"
      aria-label={ariaLabel}
    >
      <style>{`
        .pattern-loading-animation-svg {
          position: relative;
          display: block;
          aspect-ratio: 1 / 1;
        }

        .pattern-loading-animation-svg svg {
          width: 100%;
          height: 100%;
          overflow: visible;
        }

        .pattern-loading-animation-svg .anim-axis-exp {
          animation: anim-axis-exp 6s infinite ease-in-out;
        }

        .pattern-loading-animation-svg .anim-layer-bottom-exp {
          animation: anim-layer-bottom-exp 6s infinite ease-in-out;
        }

        .pattern-loading-animation-svg .anim-layer-middle-exp {
          animation: anim-layer-middle-exp 6s infinite ease-in-out;
        }

        .pattern-loading-animation-svg .anim-layer-top-exp {
          animation: anim-layer-top-exp 6s infinite ease-in-out;
        }

        .pattern-loading-animation-svg .anim-dot-exp {
          animation: anim-dot-exp 6s infinite ease-in-out;
          transform-origin: 50px 38px;
        }

        @keyframes anim-axis-exp {
          0%, 100% {
            opacity: 0;
          }
          10%, 90% {
            opacity: 1;
          }
        }

        @keyframes anim-layer-bottom-exp {
          0%, 100% {
            transform: translateY(15px);
            opacity: 0;
          }
          10%, 90% {
            transform: translateY(0);
            opacity: 1;
          }
        }

        @keyframes anim-layer-middle-exp {
          0%, 5%, 95%, 100% {
            transform: translateY(15px);
            opacity: 0;
          }
          15%, 85% {
            transform: translateY(0);
            opacity: 1;
          }
        }

        @keyframes anim-layer-top-exp {
          0%, 10%, 90%, 100% {
            transform: translateY(15px);
            opacity: 0;
          }
          20%, 80% {
            transform: translateY(0);
            opacity: 1;
          }
        }

        @keyframes anim-dot-exp {
          0%, 15%, 85%, 100% {
            transform: scale(0);
            opacity: 0;
          }
          25% {
            transform: scale(1.3);
            opacity: 1;
          }
          30%, 75% {
            transform: scale(1);
            opacity: 1;
          }
        }
      `}</style>

      <svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <g className="anim-axis-exp">
          <path
            d="M 50 15 L 50 85"
            stroke="#90a4ae"
            strokeWidth="1"
            strokeOpacity="0.5"
            strokeLinecap="round"
            strokeDasharray="2 4"
          />
        </g>

        <path
          className="anim-layer-bottom-exp"
          d="M 50 62 L 82 78 L 50 94 L 18 78 Z"
          stroke="#90a4ae"
          strokeWidth="1.5"
          strokeOpacity="0.4"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <path
          className="anim-layer-middle-exp"
          d="M 50 42 L 82 58 L 50 74 L 18 58 Z"
          stroke="#90a4ae"
          strokeWidth="1.5"
          strokeOpacity="0.7"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <path
          className="anim-layer-top-exp"
          d="M 50 22 L 82 38 L 50 54 L 18 38 Z"
          fill="#e2e8ea"
          stroke="#90a4ae"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <g className="anim-dot-exp">
          <circle cx="50" cy="38" r="3.5" fill="#00d4ff" />
        </g>
      </svg>
    </div>
  );
}
