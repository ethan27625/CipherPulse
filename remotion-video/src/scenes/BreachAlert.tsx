import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, Easing, spring } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

export const BreachAlert: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.red;
  const keyword = scene.keyword ?? "BREACH";

  // Alert pulse every 30 frames
  const pulsePhase = frame % 30;
  const alertPulse = interpolate(pulsePhase, [0, 15, 30], [1, 0.4, 1]);

  const alertScale = spring({
    frame,
    fps,
    config: { damping: 10, stiffness: 200, mass: 0.8 },
    from: 0.5,
    to: 1,
  });

  const glitchOffset = frame % 7 === 0 ? (Math.random() - 0.5) * 6 : 0;

  const warningLines = [
    "⚠ UNAUTHORIZED ACCESS DETECTED",
    "⚠ SYSTEM COMPROMISED",
    "⚠ DATA EXFILTRATION ACTIVE",
  ];

  return (
    <div
      style={{
        width,
        height,
        background: COLORS.black,
        position: "relative",
        overflow: "hidden",
        fontFamily: "'Courier New', monospace",
      }}
    >
      {/* Red scan overlay */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `${accent}0A`,
          animation: "none",
          opacity: alertPulse,
        }}
      />

      {/* Corner brackets */}
      {[
        { top: 40, left: 40 },
        { top: 40, right: 40 },
        { bottom: 40, left: 40 },
        { bottom: 40, right: 40 },
      ].map((pos, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            ...pos,
            width: 60,
            height: 60,
            borderTop: i < 2 ? `3px solid ${accent}` : "none",
            borderBottom: i >= 2 ? `3px solid ${accent}` : "none",
            borderLeft: i % 2 === 0 ? `3px solid ${accent}` : "none",
            borderRight: i % 2 === 1 ? `3px solid ${accent}` : "none",
            opacity: alertPulse,
          }}
        />
      ))}

      {/* Main keyword */}
      <div
        style={{
          position: "absolute",
          top: "18%",
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          transform: `scale(${alertScale}) translateX(${glitchOffset}px)`,
        }}
      >
        <p
          style={{
            color: accent,
            fontSize: 110,
            fontWeight: 900,
            letterSpacing: 8,
            textShadow: `0 0 30px ${accent}, 0 0 60px ${accent}44`,
            margin: 0,
          }}
        >
          {keyword}
        </p>
      </div>

      {/* Warning ticker lines */}
      <div
        style={{
          position: "absolute",
          top: "38%",
          left: 0,
          right: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
        }}
      >
        {warningLines.map((line, i) => (
          <p
            key={i}
            style={{
              color: accent,
              fontSize: 22,
              fontWeight: 700,
              opacity: frame > i * 8 ? alertPulse * 0.8 : 0,
              margin: 0,
              letterSpacing: 2,
            }}
          >
            {line}
          </p>
        ))}
      </div>

      {/* Caption — y=1300, right:160 keeps text left of x=920 (YouTube buttons x=820-1080) */}
      <p
        style={{
          position: "absolute",
          top: 1300,
          left: 60,
          right: 160,
          color: COLORS.text,
          fontSize: 52,
          fontWeight: 900,
          textAlign: "center",
          lineHeight: 1.25,
          textShadow: "0 2px 12px rgba(0,0,0,0.9)",
          margin: 0,
        }}
      >
        {scene.caption}
      </p>
    </div>
  );
};
