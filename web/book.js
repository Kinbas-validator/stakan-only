// Расчёты страницы без DOM: раскладка стакана по ордерам, рыночная заявка, последняя заявка, итоги недели.
// В браузере — глобальный объект PB, в Node — module.exports (тесты: node --test tests/).
(function (root) {
  const tierOfFee = f => !f ? "standard" : (f === 50 ? "plus" : "premium");

  // Живой уровень (WS) раскладывается по ордерам из REST в порядке очереди; остаток — «?» в конце очереди.
  // levels: Map цена (строка) -> размер; orders: ордера стороны из orderBookOrders; tierOf(аккаунт) -> тариф.
  function sideRows(levels, orders, side, nLevels, tierOf) {
    const lv = [...levels.entries()].sort((a, b) => side === "asks" ? a[0] - b[0] : b[0] - a[0]).slice(0, nLevels);
    const byPrice = new Map();
    for (const o of orders || []) { (byPrice.get(o.price) || byPrice.set(o.price, []).get(o.price)).push(o); }
    const rows = []; let cum = 0;
    for (const [price, size] of lv) {
      let left = size, q = 0;
      for (const o of byPrice.get(price) || []) {
        if (left <= 1e-9) break;
        const part = Math.min(+o.remaining_base_amount, left); left -= part; cum += part;
        const acc = String(o.owner_account_index);
        rows.push({ price, size: part, usd: part * price, tier: tierOf(acc), acc, q: ++q, cum });
      }
      if (left > 1e-7) { cum += left; rows.push({ price, size: left, usd: left * price, tier: "unknown", acc: "?", q: ++q, cum }); }
    }
    return rows;
  }

  // Рыночная заявка на usd: какие строки заберёт и какая доля уйдёт premium.
  function simulate(rows, usd) {
    let need = usd, got = 0, prem = 0, base = 0; const hit = new Set();
    for (let i = 0; i < rows.length && need > 1e-9; i++) {
      const r = rows[i];
      if (r.tier === "mine") continue;  // со своими ордерами не торгуем
      const take = Math.min(r.usd, need);
      need -= take; got += take; base += take / r.price; hit.add(i);
      if (r.tier === "premium") prem += take;
    }
    let run = 0, runPrice = null;
    for (const r of rows) { if (r.tier === "mine") continue; if (r.tier !== "premium") break; run += r.usd; runPrice = r.price; }
    const best = rows.length ? rows[0].price : null;
    return { got, prem, share: got ? 100 * prem / got : 0, full: need <= 1e-6, avg: base ? got / base : null,
             slip: best && base ? Math.abs(got / base - best) / best * 100 : 0, hit, run, runPrice };
  }

  // Сделки сервера + живые из публичной ленты, которых на сервере ещё нет (или там комиссия ещё неизвестна).
  function mergeTrades(server, live) {
    const byId = new Map(server.map(t => [t.trade_id, t]));
    for (const [id, t] of live) { const s = byId.get(id); if (!s || s.cp_tier === "unknown") byId.set(id, t); }
    return [...byId.values()].sort((a, b) => b.ts - a.ts);
  }

  // Неделя: серверные итоги + живые сделки, которых сервер ещё не учёл.
  function weekTotals(week, server, live) {
    const w = { usd: week.usd, prem: week.premium_usd };
    const byId = new Map(server.map(t => [t.trade_id, t]));
    for (const [id, t] of live) {
      if (t.ts < week.start_ms) continue;
      const s = byId.get(id);
      if (!s) { w.usd += t.usd; if (t.cp_tier === "premium") w.prem += t.usd; }
      else if (s.cp_tier === "unknown" && t.cp_tier === "premium") w.prem += t.usd;
    }
    w.pct = w.usd ? 100 * w.prem / w.usd : null;
    return w;
  }

  // Последняя заявка = все исполнения моего самого свежего ордера (all — свежие сверху).
  function lastOrder(all) {
    if (!all.length) return null;
    const ord = all[0].my_order, fills = all.filter(t => t.my_order === ord);
    const usd = fills.reduce((s, t) => s + t.usd, 0), prem = fills.filter(t => t.cp_tier === "premium").reduce((s, t) => s + t.usd, 0);
    const pct = usd ? 100 * prem / usd : 0, unknown = fills.some(t => t.cp_tier === "unknown");
    return { fills, usd, prem, pct, unknown, first: fills[0],
             verdict: unknown ? "check" : pct >= 99.5 ? "all" : pct > 0 ? "part" : "none" };
  }

  // Моя сделка из публичной ленты: комиссия контрагента в ней видна сразу.
  function liveTrade(t, me) {
    const iAmAsk = String(t.ask_account_id) === me, makerAsk = t.is_maker_ask, iAmMaker = iAmAsk === makerAsk;
    const cpFee = iAmMaker ? t.taker_fee : t.maker_fee;
    return {
      trade_id: t.trade_id, ts: t.timestamp, symbol: t.market_symbol, side: iAmAsk ? "sell" : "buy",
      my_role: iAmMaker ? "maker" : "taker", price: t.price, size: t.size, usd: +t.usd_amount,
      counterparty: iAmAsk ? t.bid_account_id : t.ask_account_id, cp_fee: cpFee || 0, cp_tier: tierOfFee(cpFee),
      my_order: iAmAsk ? t.ask_id : t.bid_id, live: true,
    };
  }

  const api = { tierOfFee, sideRows, simulate, mergeTrades, weekTotals, lastOrder, liveTrade };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.PB = api;
})(this);
