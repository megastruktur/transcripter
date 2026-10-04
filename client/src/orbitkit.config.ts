import { defineConfig, validateConfig } from "@orbitkit/ui";
import rawConfig from "./orbitkit.config.json";

const config = defineConfig(rawConfig as Parameters<typeof defineConfig>[0]);

const result = validateConfig(config);
if (!result.ok) {
  throw new Error(`orbitkit.config: ${result.errors.join(", ")}`);
}

export default config;
