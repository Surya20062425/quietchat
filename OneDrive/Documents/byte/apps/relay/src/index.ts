import { config, isDevAuth } from "./config.js";
import { createRelay } from "./server.js";

const relay = createRelay();

relay.httpServer.listen(config.port, () => {
  console.log(`[relay] listening on :${config.port}`);
  console.log(`[relay] origins: ${config.allowedOrigins.join(", ")}`);
  console.log(`[relay] queue TTL: ${Math.round(config.queueTtlMs / 3_600_000)}h`);
  if (isDevAuth) {
    console.warn(
      "[relay] DEV AUTH: accepting bare userIds (no token verification). " +
        "Set RELAY_DEV_SECRET before any real deployment.",
    );
  }
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    relay.close().finally(() => process.exit(0));
  });
}
