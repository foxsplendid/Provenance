"use strict";

const fs = require("node:fs");
const path = require("node:path");
const zipStore = require(path.join(__dirname, "..", "src", "lib", "zip-store.js"));

if (process.argv.length !== 3) {
  process.stderr.write("Usage: node make_zip_fixture.js OUTPUT.zip\n");
  process.exit(2);
}

const entries = [
  { name: "plain.json", data: "{\"ok\":true}\n" },
  { name: "对话-🌟.json", data: "{\"message\":\"你好，世界 🌍\"}\n" },
  { name: "nested/emoji-😀.json", data: new TextEncoder().encode("{\"value\":\"café\"}\n") }
];

if (zipStore.crc32("123456789") !== 0xcbf43926) {
  throw new Error("CRC-32 standard vector failed.");
}

for (const invalidName of ["bad-\ud800.txt", "bad-\udc00.txt"]) {
  let rejected = false;
  try {
    zipStore.createZip([{ name: invalidName, data: "invalid" }]);
  } catch (error) {
    rejected = error && error.code === "INVALID_NAME";
  }
  if (!rejected) {
    throw new Error("Malformed Unicode ZIP name was not rejected.");
  }
}

fs.writeFileSync(process.argv[2], zipStore.createZip(entries));
