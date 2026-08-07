import {
  runtimeConfigurationFromEnvironment,
  startOAuthServer,
} from "./runtime.js";

const runtime = await startOAuthServer(
  runtimeConfigurationFromEnvironment(process.env),
);
async function stop(): Promise<void> {
  try {
    await runtime.close();
  } catch {
    process.exitCode = 1;
  }
}

process.once("SIGINT", stop);
process.once("SIGTERM", stop);
