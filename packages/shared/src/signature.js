import { createHmac, timingSafeEqual } from "node:crypto";

export function signGitHubBody(rawBody, secret) {
  const digest = createHmac("sha256", secret).update(rawBody).digest("hex");
  return `sha256=${digest}`;
}

export function verifyGitHubSignature(rawBody, signatureHeader, secret) {
  if (!secret) {
    return { ok: true, reason: "signature verification disabled" };
  }

  if (!signatureHeader || !signatureHeader.startsWith("sha256=")) {
    return { ok: false, reason: "missing x-hub-signature-256 header" };
  }

  const expected = signGitHubBody(rawBody, secret);
  const given = Buffer.from(signatureHeader, "utf8");
  const wanted = Buffer.from(expected, "utf8");

  if (given.length !== wanted.length) {
    return { ok: false, reason: "signature length mismatch" };
  }

  return timingSafeEqual(given, wanted)
    ? { ok: true, reason: "signature matched" }
    : { ok: false, reason: "signature mismatch" };
}
