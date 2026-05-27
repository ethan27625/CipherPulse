import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  Sequence,
  Audio,
  staticFile,
} from "remotion";
import { COLORS } from "./colors";
import { SceneData } from "./types";
import { CyberGrid } from "./scenes/CyberGrid";
import { DataStream } from "./scenes/DataStream";
import { BreachAlert } from "./scenes/BreachAlert";
import { TerminalRain } from "./scenes/TerminalRain";
import { CityScan } from "./scenes/CityScan";
import { PulseWave } from "./scenes/PulseWave";
import { GENERATED_SCENE_REGISTRY } from "./scenes/generated/AllGeneratedScenes";

export interface CompositionProps {
  scenes: SceneData[];
  title: string;
  hook: string;
  musicFile?: string;
}

function SceneComponent({ scene }: { scene: SceneData }) {
  // Use a custom-generated component if scene_director produced one for this scene.
  // Falls back to the static template when the registry has no entry (generation
  // failed or tsc validation rejected the file).
  const Generated = GENERATED_SCENE_REGISTRY[scene.id];
  if (Generated) {
    return <Generated scene={scene} />;
  }

  switch (scene.type) {
    case "CyberGrid":
      return <CyberGrid scene={scene} />;
    case "DataStream":
      return <DataStream scene={scene} />;
    case "BreachAlert":
      return <BreachAlert scene={scene} />;
    case "TerminalRain":
      return <TerminalRain scene={scene} />;
    case "CityScan":
      return <CityScan scene={scene} />;
    case "PulseWave":
      return <PulseWave scene={scene} />;
    default:
      return <CyberGrid scene={scene} />;
  }
}

export const CipherPulseComposition: React.FC<CompositionProps> = ({
  scenes,
  title,
  hook,
  musicFile,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  let cumulative = 0;
  const sceneStarts: number[] = scenes.map((s) => {
    const start = cumulative;
    cumulative += Math.round(s.duration_seconds * fps);
    return start;
  });

  return (
    <AbsoluteFill style={{ background: COLORS.black }}>
      {/* Background music at low volume */}
      {musicFile && (
        <Audio src={staticFile(musicFile)} volume={0.2} />
      )}

      {/* Scene sequences */}
      {scenes.map((scene, i) => {
        const durationFrames = Math.round(scene.duration_seconds * fps);
        return (
          <Sequence
            key={i}
            from={sceneStarts[i]}
            durationInFrames={durationFrames}
          >
            <SceneComponent scene={scene} />
          </Sequence>
        );
      })}

      {/* CipherPulse watermark — safe zone: y≈1575 (above YouTube bottom overlay y=1620),
          x≈910 end (left of YouTube right buttons x=820-1080 — text is short so fits) */}
      <div
        style={{
          position: "absolute",
          bottom: 345,
          right: 40,
          color: COLORS.cyan,
          opacity: 0.35,
          fontSize: 22,
          fontWeight: 900,
          letterSpacing: 3,
          fontFamily: "monospace",
        }}
      >
        CIPHERPULSE
      </div>
    </AbsoluteFill>
  );
};
