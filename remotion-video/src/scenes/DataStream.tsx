import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

const CHARS = "01アイウエオカキクケコABCDEF<>{}[]()".split("");
const COLUMNS = 22;

function seededRand(seed: number): number {
  const x = Math.sin(seed + 1) * 10000;
  return x - Math.floor(x);
}

export const DataStream: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.cyan;

  const colW = width / COLUMNS;

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
      {/* Falling character columns */}
      {Array.from({ length: COLUMNS }).map((_, col) => {
        const speed = 0.8 + seededRand(col * 7) * 1.4;
        const offset = seededRand(col * 13) * 60;
        const charCount = 12 + Math.floor(seededRand(col * 3) * 10);
        const xPos = col * colW + colW / 2;

        return Array.from({ length: charCount }).map((_, row) => {
          const charFrame = (frame * speed + offset + row * 8) % (height + 200);
          const yPos = charFrame - 100;
          const isHead = row === 0;
          const opacity = isHead ? 1 : Math.max(0, 1 - row / charCount);
          const charIdx =
            Math.floor(seededRand(col * 100 + row + frame * 0.3) * CHARS.length);

          return (
            <div
              key={`${col}-${row}`}
              style={{
                position: "absolute",
                left: xPos - 10,
                top: yPos,
                color: isHead ? COLORS.white : accent,
                opacity,
                fontSize: 18,
                fontWeight: isHead ? 700 : 400,
                textShadow: isHead ? `0 0 8px ${accent}` : "none",
              }}
            >
              {CHARS[charIdx]}
            </div>
          );
        });
      })}

      {/* Dark overlay for readability */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(to bottom, transparent 20%, #06060988 50%, transparent 80%)",
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
          background: `${COLORS.black}CC`,
          padding: "16px 24px",
          borderRadius: 10,
        }}
      >
        {scene.caption}
      </p>
    </div>
  );
};
