// Логика страницы: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const PB = require("../web/book.js");

const reg = { "1": "premium", "2": "standard", "3": "plus" };
const tierOf = a => a === "9" ? "mine" : (reg[a] || "unknown");
const order = (price, amount, owner) => ({ price, remaining_base_amount: String(amount), owner_account_index: owner });

test("tierOfFee", () => {
  assert.equal(PB.tierOfFee(undefined), "standard");
  assert.equal(PB.tierOfFee(0), "standard");
  assert.equal(PB.tierOfFee(50), "plus");
  assert.equal(PB.tierOfFee(84), "premium");
});

test("sideRows: уровни раскладываются по ордерам в порядке очереди, остаток — «?»", () => {
  const asks = new Map([["10.0", 3], ["10.5", 1], ["11.0", 5]]);
  const orders = [order("10.0", 1, 1), order("10.0", 1.5, 2), order("10.5", 4, 3)];
  const rows = PB.sideRows(asks, orders, "asks", 2, tierOf);
  assert.deepEqual(rows.map(r => [r.price, r.size, r.tier, r.q]),
    [["10.0", 1, "premium", 1], ["10.0", 1.5, "standard", 2], ["10.0", 0.5, "unknown", 3], ["10.5", 1, "plus", 1]]);
  assert.equal(rows.at(-1).cum, 4);
  assert.equal(rows[0].usd, 10);
  const bids = PB.sideRows(new Map([["9", 1], ["9.5", 2]]), [], "bids", 5, tierOf);
  assert.deepEqual(bids.map(r => r.price), ["9.5", "9"]);   // покупки — от лучшей (высшей) цены
});

test("simulate: доля premium, свои ордера пропускаются, подряд premium от лучшей цены", () => {
  const rows = [
    { price: 10, usd: 100, tier: "premium" }, { price: 10, usd: 50, tier: "mine" },
    { price: 10.1, usd: 200, tier: "premium" }, { price: 10.2, usd: 300, tier: "standard" },
  ];
  const s = PB.simulate(rows, 400);
  assert.equal(s.got, 400);
  assert.equal(s.prem, 300);
  assert.equal(s.share, 75);
  assert.ok(s.full);
  assert.deepEqual([...s.hit], [0, 2, 3]);
  assert.equal(s.run, 300);
  assert.equal(s.runPrice, 10.1);
  const big = PB.simulate(rows, 10000);
  assert.ok(!big.full);
  assert.equal(big.got, 600);
  assert.equal(PB.simulate([], 100).share, 0);
});

const T = (id, ts, tier, extra = {}) => ({ trade_id: id, ts, usd: 100, cp_tier: tier, my_order: id, ...extra });

test("mergeTrades: живая сделка заменяет серверную без комиссии и добавляет новую", () => {
  const server = [T(1, 1000, "standard"), T(2, 2000, "unknown")];
  const live = new Map([[2, T(2, 2000, "premium", { live: true })], [3, T(3, 3000, "premium", { live: true })]]);
  const all = PB.mergeTrades(server, live);
  assert.deepEqual(all.map(t => [t.trade_id, t.cp_tier]), [[3, "premium"], [2, "premium"], [1, "standard"]]);
});

test("weekTotals: живые сделки досчитываются, старые и уже учтённые — нет", () => {
  const week = { usd: 300, premium_usd: 100, start_ms: 1000 };
  const server = [T(1, 1500, "premium"), T(2, 1600, "unknown"), T(4, 1700, "standard")];
  const live = new Map([[2, T(2, 1600, "premium")], [3, T(3, 1800, "premium")], [5, T(5, 500, "premium")], [4, T(4, 1700, "standard")]]);
  const w = PB.weekTotals(week, server, live);
  assert.equal(w.usd, 400);
  assert.equal(w.prem, 300);
  assert.equal(w.pct, 75);
  assert.equal(PB.weekTotals({ usd: 0, premium_usd: 0, start_ms: 0 }, [], new Map()).pct, null);
});

test("lastOrder: все исполнения последнего ордера и вердикт", () => {
  assert.equal(PB.lastOrder([]), null);
  const fills = [T(3, 3000, "premium", { my_order: 7 }), T(2, 2000, "standard", { my_order: 7 }), T(1, 1000, "premium", { my_order: 6 })];
  let lo = PB.lastOrder(fills);
  assert.equal(lo.fills.length, 2);
  assert.equal(lo.pct, 50);
  assert.equal(lo.verdict, "part");
  assert.equal(PB.lastOrder([T(1, 1, "premium")]).verdict, "all");
  assert.equal(PB.lastOrder([T(1, 1, "standard")]).verdict, "none");
  lo = PB.lastOrder([T(1, 1, "unknown", { my_order: 5 }), T(2, 0, "premium", { my_order: 5 })]);
  assert.equal(lo.verdict, "check");
});

test("liveTrade: сторона, роль и комиссия контрагента из публичной ленты", () => {
  const ws = { trade_id: 1, timestamp: 5, market_symbol: "AAPL", price: "10", size: "2", usd_amount: "20",
               ask_account_id: 44, bid_account_id: 7, is_maker_ask: false, maker_fee: 120, ask_id: 100, bid_id: 200 };
  const t = PB.liveTrade(ws, "44");   // я продавец и тейкер: контрагент — мейкер с комиссией 120
  assert.deepEqual([t.side, t.my_role, t.cp_tier, t.counterparty, t.my_order, t.usd], ["sell", "taker", "premium", 7, 100, 20]);
  const t2 = PB.liveTrade({ ...ws, is_maker_ask: true }, "44");   // я мейкер: комиссия тейкера не указана — standard
  assert.deepEqual([t2.my_role, t2.cp_tier, t2.cp_fee], ["maker", "standard", 0]);
});
