import React from "react";
import { Composition } from "remotion";
import { CipherPulseComposition, CompositionProps } from "./CipherPulseComposition";

const DEFAULT_FPS = 30;
const WIDTH = 1080;
const HEIGHT = 1920;
const DEFAULT_DURATION_SECONDS = 58;

// Default props shown in Remotion Studio preview
const defaultProps: CompositionProps = {
  scenes: [
    {
      id: "scene-0",
      type: "CyberGrid",
      caption: "Every 39 seconds, a computer is hacked.",
      duration_seconds: 4,
      accent_color: "#00F2EA",
    },
    {
      id: "scene-1",
      type: "DataStream",
      caption: "Billions of records stolen every year.",
      duration_seconds: 4,
      accent_color: "#00F2EA",
    },
    {
      id: "scene-2",
      type: "BreachAlert",
      caption: "How do they do it?",
      duration_seconds: 4,
      accent_color: "#FF3B3B",
      keyword: "HACKED",
    },
    {
      id: "scene-3",
      type: "TerminalRain",
      caption: "They exploit one thing: your trust.",
      duration_seconds: 5,
      accent_color: "#00F2EA",
    },
    {
      id: "scene-4",
      type: "CityScan",
      caption: "Global networks attacked every second.",
      duration_seconds: 5,
      accent_color: "#00BCD4",
    },
    {
      id: "scene-5",
      type: "PulseWave",
      caption: "Stay informed. Stay protected.",
      duration_seconds: 4,
      accent_color: "#00F2EA",
    },
  ],
  title: "CipherPulse Preview",
  hook: "Every 39 seconds, a computer is hacked.",
};

export const RemotionRoot: React.FC = () => {
  const totalSeconds = defaultProps.scenes.reduce(
    (sum: number, s: { duration_seconds: number }) => sum + s.duration_seconds,
    0
  );

  return (
    <Composition
      id="CipherPulse"
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      component={CipherPulseComposition as any}
      durationInFrames={Math.round(totalSeconds * DEFAULT_FPS)}
      fps={DEFAULT_FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={defaultProps as unknown as Record<string, unknown>}
      calculateMetadata={async ({ props }: { props: Record<string, unknown> }) => {
        const scenes = (props["scenes"] as Array<{ duration_seconds: number }>) ?? [];
        const totalSecs = scenes.reduce(
          (sum: number, s: { duration_seconds: number }) => sum + s.duration_seconds,
          0
        );
        return {
          durationInFrames: Math.round(totalSecs * DEFAULT_FPS),
        };
      }}
    />
  );
};
