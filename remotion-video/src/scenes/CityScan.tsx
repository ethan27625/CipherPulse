import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

interface Node {
  x: number;
  y: number;
  label: string;
}

const NODES: Node[] = [
  { x: 0.15, y: 0.22, label: "NYC" },
  { x: 0.82, y: 0.18, label: "LON" },
  { x: 0.5, y: 0.38, label: "FRA" },
  { x: 0.72, y: 0.55, label: "SNG" },
  { x: 0.28, y: 0.65, label: "LAX" },
  { x: 0.6, y: 0.72, label: "TKY" },
  { x: 0.1, y: 0.45, label: "CHI" },
  { x: 0.88, y: 0.78, label: "SYD" },
];

const CONNECTIONS = [
  [0, 2], [1, 2], [2, 3], [2, 4], [3, 5], [4, 6], [5, 7], [0, 1],
];

function seededRand(seed: number): number {
  const x = Math.sin(seed + 1) * 10000;
  return x - Math.floor(x);
}

export const CityScan: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.teal;

  // Radar sweep angle
  const sweepAngle = interpolate(frame, [0, fps * 3], [0, Math.PI * 2], {
    extrapolateRight: "wrap",
  });

  return (
    <div
      style={{
        width,
        height,
        background: `radial-gradient(ellipse at 50% 40%, #0D1B2A 0%, ${COLORS.black} 70%)`,
        position: "relative",
        overflow: "hidden",
        fontFamily: "monospace",
      }}
    >
      {/* Network edges */}
      <svg
        width={width}
        height={height}
        style={{ position: "absolute", top: 0, left: 0 }}
      >
        {CONNECTIONS.map(([a, b], i) => {
          const na = NODES[a];
          const nb = NODES[b];
          const packetProgress = (frame / fps + i * 0.3) % 1;

          const px = na.x * width + (nb.x - na.x) * width * packetProgress;
          const py = na.y * height + (nb.y - na.y) * height * packetProgress;

          return (
            <React.Fragment key={i}>
              <line
                x1={na.x * width}
                y1={na.y * height}
                x2={nb.x * width}
                y2={nb.y * height}
                stroke={accent}
                strokeWidth={1}
                opacity={0.25}
              />
              {/* Travelling data packet */}
              <circle cx={px} cy={py} r={4} fill={accent} opacity={0.8} />
            </React.Fragment>
          );
        })}

        {/* City nodes */}
        {NODES.map((node, i) => {
          const ping = 1 - (frame % 30) / 30;
          const isActive = seededRand(i + Math.floor(frame / 20)) > 0.5;
          const nodeColor = isActive ? accent : COLORS.blue;

          return (
            <React.Fragment key={i}>
              <circle
                cx={node.x * width}
                cy={node.y * height}
                r={14 + ping * 8}
                fill="none"
                stroke={nodeColor}
                strokeWidth={1.5}
                opacity={ping * 0.5}
              />
              <circle
                cx={node.x * width}
                cy={node.y * height}
                r={6}
                fill={nodeColor}
                opacity={0.9}
              />
              <text
                x={node.x * width + 12}
                y={node.y * height - 10}
                fill={nodeColor}
                fontSize={16}
                fontWeight="700"
                opacity={0.85}
              >
                {node.label}
              </text>
            </React.Fragment>
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
          background: `${COLORS.black}BB`,
          padding: "16px 24px",
          borderRadius: 10,
        }}
      >
        {scene.caption}
      </p>
    </div>
  );
};
