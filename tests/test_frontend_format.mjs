// Teste N: o frontend deve mostrar "118.700 MHz", nunca "118700.000 MHz".
// Extrai a função pura formatFrequencyMhz() de static/app.js (sem precisar
// de DOM/navegador) e valida a formatação.
//
// Rodar com: node tests/test_frontend_format.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

const match = src.match(/function formatFrequencyMhz\([^)]*\)\s*{[\s\S]*?\n}/);
assert.ok(match, "formatFrequencyMhz não encontrada em static/app.js");

// eslint-disable-next-line no-new-func
const formatFrequencyMhz = new Function(`${match[0]}; return formatFrequencyMhz;`)();

assert.equal(formatFrequencyMhz(118.7), "118.700");
assert.equal(formatFrequencyMhz(127.45), "127.450");
assert.equal(formatFrequencyMhz(121.7), "121.700");
assert.equal(formatFrequencyMhz(122.5), "122.500");
assert.equal(formatFrequencyMhz(121.5), "121.500");
assert.equal(formatFrequencyMhz(122.8), "122.800");
assert.equal(formatFrequencyMhz(null), "---.---");
assert.equal(formatFrequencyMhz(undefined), "---.---");

// o bug relatado: um valor de 118.7 MHz NUNCA pode aparecer formatado como
// "118700.000" (o que aconteceria se algo multiplicasse por 1000 no caminho)
assert.notEqual(formatFrequencyMhz(118.7), "118700.000");
assert.ok(!formatFrequencyMhz(118.7).includes("118700"));

console.log("OK - formatFrequencyMhz: todos os casos passaram (N)");
