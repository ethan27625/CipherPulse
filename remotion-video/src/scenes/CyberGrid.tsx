import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

export const CyberGrid: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.cyan;

  const pulse = interpolate(
    Math.sin((frame / fps) * Math.PI * 2),
    [-1, 1],
    [0.4, 1.0]
  );

  const gridLines = 20;
  const cellW = width / gridLines;
  const cellH = height / gridLines;

  const scanY = interpolate(frame, [0, 90], [0, height], {
    extrapolateRight: "clamp",
    easing: Easing.inOut(Easing.ease),
  });

  return (
    <div
      style={{
        width,
        height,
        background: COLORS.black,
        position: "relative",
        overflow: "hidden",
        fontFamily: "monospace",
      }}
    >
      {/* Grid lines */}
      <svg
        width={width}
        height={height}
        style={{ position: "absolute", top: 0, left: 0, opacity: 0.18 }}
      >
        {Array.from({ length: gridLines + 1 }).map((_, i) => (
          <React.Fragment key={i}>
            <line
              x1={i * cellW}
              y1={0}
              x2={i * cellW}
              y2={height}
              stroke={accent}
              strokeWidth={0.5}
            />
            <line
              x1={0}
              y1={i * cellH}
              x2={width}
              y2={i * cellH}
              stroke={accent}
              strokeWidth={0.5}
            />
          </React.Fragment>
        ))}
        {/* Horizontal scan line */}
        <line
          x1={0}
          y1={scanY}
          x2={width}
          y2={scanY}
          stroke={accent}
          strokeWidth={2}
          opacity={0.6}
        />
      </svg>

      {/* Radial glow centre */}
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 400,
          height: 400,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${accent}22 0%, transparent 70%)`,
          opacity: pulse,
        }}
      />

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
