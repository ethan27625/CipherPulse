export interface SceneData {
  id: string;
  type: "CyberGrid" | "DataStream" | "BreachAlert" | "TerminalRain" | "CityScan" | "PulseWave";
  caption: string;
  duration_seconds: number;
  accent_color?: string;
  keyword?: string;
}
