import { useVideoConfig, staticFile } from "remotion";
import { useMemo } from "react";

export interface SceneData {
  id: string;
  type: "CyberGrid" | "DataStream" | "BreachAlert" | "TerminalRain" | "CityScan" | "PulseWave";
  caption: string;
  duration_seconds: number;
  accent_color?: string;
  keyword?: string;
}

export interface VideoProps {
  scenes: SceneData[];
  title: string;
  hook: string;
}

export function useSceneData(): VideoProps {
  const { fps } = useVideoConfig();

  const raw = useMemo(() => {
    try {
      // scene_data.json is written by remotion_generator.py before render
      const url = staticFile("scene_data.json");
      // In server-side rendering, this is synchronously available via Remotion's bundle
      return require(url) as VideoProps;
    } catch {
      return {
        scenes: [],
        title: "CipherPulse",
        hook: "",
      } as VideoProps;
    }
  }, [fps]);

  return raw;
}
