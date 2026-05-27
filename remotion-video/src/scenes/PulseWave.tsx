import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

const WAVE_COUNT = 5;

export const PulseWave: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.cyan;

  const t = frame / fps;

  // Generate waveform path
  function buildWavePath(
    amplitude: number,
    frequency: number,
    phaseOffset: number,
    yOffset: number
  ): string {
    const points: string[] = [];
    const steps = 200;
    for (let i = 0; i <= steps; i++) {
      const x = (i / steps) * width;
      const phase = (x / width) * Math.PI * 2 * frequency + t * 3 + phaseOffset;
      const y = yOffset + Math.sin(phase) * amplitude;
      points.push(`${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`);
    }
    return points.join(" ");
  }

  const centerY = height * 0.5;

  return (
    <div
      style={{
        width,
        height,
        background: `linear-gradient(180deg, ${COLORS.surface} 0%, ${COLORS.black} 100%)`,
        position: "relative",
        overflow: "hidden",
      }}
    >
      <svg
        width={width}
        height={height}
        style={{ position: "absolute", top: 0, left: 0 }}
      >
        {Array.from({ length: WAVE_COUNT }).map((_, i) => {
          const alpha = 0.6 - i * 0.1;
          const amplitude = 80 - i * 12;
          const freq = 2 + i * 0.5;
          const phase = (i * Math.PI) / WAVE_COUNT;
          const yOff = centerY + (i - WAVE_COUNT / 2) * 40;

          return (
            <path
              key={i}
              d={buildWavePath(amplitude, freq, phase, yOff)}
              stroke={accent}
              strokeWidth={2.5 - i * 0.3}
              fill="none"
              opacity={alpha}
            />
          );
        })}

        {/* Vertical pulse bar */}
        {Array.from({ length: 12 }).map((_, i) => {
          const barH = 40 + Math.abs(Math.sin(t * 4 + i)) * 160;
          const x = (i / 11) * (width - 80) + 40;
          return (
            <rect
              key={i}
              x={x - 6}
              y={centerY - barH / 2}
              width={12}
              height={barH}
              rx={4}
              fill={accent}
              opacity={0.35}
            />
          );
        })}
      </svg>

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
