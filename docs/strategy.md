# Options Portfolio — Strategy and Execution

*The operating manual the desk agents enforce. Part A is strategy (A1–A5); Part B is execution (B1–B3). Converted from the Word original of 22 Sep 2026.*

**Options Portfolio — Strategy and Execution**

ETH and FIL options book

22 September 2026 · Lucas Lemos

For: Lucas Lemos, Chris Bocorum

## **Introduction**

**Why this exists. **The portfolio is down roughly US$20M on ETH and US$20M on FIL, and the mark has got worse for three months running. The price has largely gone sideways in that time, which is the one environment this book is not built for, and every adjustment we have made has cost us fees on the way in and on the way out.

We are not short of tools. Between us we have built the position view, the scenario grid, the per-trade curve, the counterparty pricing models and the optimizer. What we do not have is an agreed way of w them: when we look, what we look at, what triggers a trade, who is allowed to do it, and what happens in the hours neither of us is at a screen.

**What this document is. **The operating manual for the two of us on this book. It sets out what we decide and when, what to do when the price moves, which instrument to use, who can act without asking, and how the day is covered between London and New York.

**What it is not. **It is not a forecast and it is not advice to buy or sell anything specific. It asks us for exactly one market judgement — in A1 — and everything after that follows from the answer.

**Scope. **The ETH and FIL options books, plus the perp and forward overlay we are adding to trade direction more cheaply.

**A note on the numbers. **Every figure here — prices, sizes, limits — is an example, written so you can see the shape of the decision rather than argue about a blank. They all get replaced with our real numbers before any of it goes live.

**Where to start. **If you read one thing, read A1. If the answer there is “no, we do not believe in a big move”, then this document is managing a book we should be reshaping first.

## **How this document is organised**

Two parts, because strategy and execution are different jobs and mixing them is what makes a process unusable. Here is the line between them.

|  | **Strategy** | **Execution** |
| --- | --- | --- |
| What it answers | What should the book look like, and why | What do I do right now |
| When it happens | Monday morning, away from the screen | During the day, at the screen |
| Who | Lucas and Chris together | Whoever has the book |
| How much judgement | All of it | None |
| Changes how often | Weekly, or when the view changes | Never during the week |

Example. “Should we be holding this much upside at all?” — strategy, Monday, both of us. “ETH just touched 3,300, what do I do?” — execution, right now, Lucas alone, no call needed.

Every number in this document is an example so you can see the shape of the decision. Replace them with ours before we start.

**The test: **if you have to think about it while the market is moving, it belongs in strategy and we failed to decide it in advance. Execution should feel like reading a bus timetable.

**Part A is strategy **— the view, the policy, the limits. Thinking, done once a week.

**Part B is execution **— the levels, who acts, the cover, how we deal. No thinking, just doing.

## **Part A — ****Strategy**

### **A1. The view**

**One question decides everything else: **do we believe ETH and FIL make a big move in the next three months?

The book we hold today only pays if they do. If we do not believe it, we are paying for something we do not expect, every day, and no amount of good execution fixes that.

Three possible answers and what each means for the shape of the book:

| **Our view** | **What the book should hold** | **What we sell to pay for it** |
| --- | --- | --- |
| Big move coming, direction unknown | Keep the current shape. It is built for this. | Nothing — we accept the daily cost |
| Big move up | Upside close to the money, protection further away | The far-out downside we are not using |
| Range-bound, nothing happens | A much smaller position, closer to the money, shorter dated | The far-out strikes on both sides — they are the rent we are paying |

Example. Monday, we conclude ETH stays between 2,600 and 3,400 for the quarter — range-bound. That answer means we sell the far-out June calls and the far-out June puts we are holding for a move we no longer expect, and keep a tighter position around 3,000. That is a reshape: one project, done once. It is not something Lucas does on a Tuesday afternoon.

We write the answer down on Monday with a date on it. It does not change during the week because the price moved — only because the reasoning changed.

Alongside it, two numbers per asset: the price range we expect over 90 days, and the date by which we say the view was right or wrong.

Everything in Part B is mechanical once this is settled. Everything in Part B is guesswork if it is not.

### **A2. The policy**

This is where we decide, once, what each kind of move earns. It is a strategy decision because it is about what we want; Part B just carries it out.

| **Kind of move** | **Our response** | **Why** |
| --- | --- | --- |
| Grinds up or drifts down slowly | Nothing | Small moves are noise. Trading them costs fees and achieves nothing |
| Moves up hard | Sell some of the winning upside, use the money to buy protection, raise the floor | A rally is when protection is cheapest and our gains are most exposed |
| Moves down hard | Take profit on the protection that worked, use the money to buy longer-dated upside | A selloff is when upside is cheapest |
| Nothing happens for weeks | Sell the far-out strikes we hold for a move that is not coming | They are pure cost if we do not believe in the move |

Example — “moves up hard”. ETH goes from 3,000 to 3,600 in five days. Policy says: sell some of the winning upside, use the money to buy protection, raise the floor.

In practice that is: sell a quarter of the calls that are now in the money, collect roughly US$800k of premium, spend about US$750k of it on puts struck around 3,200, and record the new floor at 3,200. Net cost to us: about US$50k, not US$750k. The floor has moved up from 2,700 to 3,200 and never goes back down.

What we do not do: buy new calls at 3,900 because it is going up.

Three standing policies that apply to every response above:

- **Every adjustment pays for itself. ****We sell something to buy something. A trade that costs money out of pocket needs a reason written down first.**
- **The floor only goes up. ****Once we raise the level below which the book cannot lose more, it never comes back down.**
- **Direction is traded with forwards or perps, never with options. ****Changing our exposure through options means paying a spread to the market maker every single time. This is the policy that saves the most money.**
**And one thing we have been doing that stops now: rolling strikes down to stay close to the market. **It turns a paper loss into a real one, lowers the level we need to get back to, and we pay a spread for the privilege. If a position needs more time, we extend the date — we do not move the strike. A5 sets out which rolls are allowed and which are not.

### **A3. The limits**

Set by Chris, reviewed monthly. These are the boundaries execution operates inside — nobody at the screen changes them.

| **Limit** | **What it means** | **ETH (example)** | **FIL (example)** |
| --- | --- | --- | --- |
| Book size we are managing | The underlying notional the options sit against | US$60M | US$40M |
| Max single trade Lucas does alone | Biggest position he can put on or take off without calling | US$3M notional (5% of book) | US$2M notional (5%) |
| Max cost out of pocket, per trade | Premium we can pay net, after what we sell | US$150k | US$100k |
| Max cost out of pocket, per month | Everything paid net across the month | US$400k | US$250k |
| Max forward or perp position | The biggest linear hedge allowed at any time | US$15M notional (25% of book) | US$10M notional (25%) |
| Monthly funding budget on perps | What the hedge is allowed to cost us to carry | US$60k | US$40k |
| Stop | Where we de-risk regardless of view | Spot 2,100, or a further US$5M of loss | Spot 1.10, or a further US$3M |

Example — how a limit actually bites. ETH trades down to 3,300 on a Tuesday. The target exposure says reduce by US$2.5M of notional. That is inside Lucas’s US$3M limit, costs about US$2k in fees, and does not touch the premium budget because no option is traded. Lucas does it, logs it, tells nobody until the handoff.

Same day, the reduction needed is US$5M instead. Lucas does US$3M — his limit — and calls Chris for the remaining US$2M. He does not do US$5M and explain later, and he does not do nothing because it is over his limit.

Example — the monthly budget. It is the 20th. We have already paid US$380k net this month on ETH. A trade that would cost another US$60k is over the US$400k budget, so it waits for the 1st or it goes to Chris. This is the line that stops a bad month becoming a bad quarter.

**Which instruments we allow at all. **Forwards and perps for direction. Options for shape. Nothing else without a conversation — no new product types, no new counterparties, no structures neither of us can price ourselves.

**The stop deserves its own paragraph. **It is the loss level at which we reduce the book whatever we believe at the time. It is written now, while nothing is urgent, because that is the only moment anyone writes an honest one. It does not get moved on a bad day. If we want to change it, we change it on a Monday with both of us present.

**One measure of whether any of this is working: **fees paid and time-decay cost, month against month. If both are not falling, we change the policy rather than defend it.

### **A4. Which instrument for which job**

This is the rule execution reads off. Every row in Part B names an instrument, and it comes from here.

| **What we are trying to do** | **Instrument** | **Why this one** |
| --- | --- | --- |
| Change our exposure up or down, nothing else | Perp or forward | Cheapest way to move direction. We pay a few basis points, not a vol spread |
| Protect a level below the market | Buy a put spread | Defined cost, defined protection, expires cleanly with nothing to unwind |
| Keep upside after banking a gain | Buy calls or a call spread, paid for by selling in-the-money calls | We recycle premium rather than writing a cheque |
| Bank a gain after a rally | Sell part of the in-the-money calls | Turns paper gain into cash that funds the protection |
| Reduce what the book costs us to hold | Sell the far-out calls and puts we are not using | Those are the rent we pay for a move we do not expect |
| Give a position more time | Roll out in expiry, same strike | Cheaper in spread than moving the strike, and it keeps the thesis |
| Get out fast in a falling, thin market | Perp first. Options only afterwards, if at all | Option exit costs explode exactly when we most need to exit |

**Perp or forward?**

|  | **Perp** | **Forward** |
| --- | --- | --- |
| Use for | Tactical — days to weeks, adjusted often | Structural — held until a specific option expiry |
| Costs | Taker fee plus funding, charged every 8 hours | Basis in the price, no ongoing funding |
| Watch | Funding can turn against us in a strong trend | Basis moves; it has to be rolled at expiry |
| Default | This is the day-to-day tool | Use when the hedge is meant to sit |

**Two things we never do.**

- **Never trade an option to change direction. ****If the only thing we want is more or less exposure, it is a perp or a forward. Trading an option means paying the market maker a vol spread for a trade that has nothing to do with vol.**
- **Never roll a strike down. ****If a position needs more time we extend the date. Moving the strike down realizes the loss, lowers the level we need to recover to, and costs a spread on top.**
Example. ETH drops 10% and our exposure is now too long. The job is “change our exposure” — row one. So Lucas sells US$2.5M of perp, cost about US$2k, done in a minute. He does not buy puts, he does not roll anything, and he does not call anyone. If instead the job were “we want a floor at 2,700 for the next three months”, that is row two — a put spread, it changes the shape of the book, and it is not his call.

### **A5. Rolls**

A roll is not a separate kind of trade. It is a sell and a buy done as one package — which means every roll is already covered by A4 and by the sizing rule in B1a. It gets its own section for two reasons: the optimizer’s main output is roll trades, so this is what we will be looking at most days, and “roll” is the word under which most of our discipline has leaked away.

**Which rolls we do and which we do not**

| **Roll** | **What it is** | **Allowed?** | **Why** |
| --- | --- | --- | --- |
| Out, same strike | Close the near expiry, open a later one, strike unchanged | YES — Chris approves | Buys time without abandoning the level. The cheapest way to stay in a view that needs longer |
| Out and up, after a gain | Close an in-the-money near leg, open a higher strike further out | YES — only as the +20% row, funded by the sale | This is banking and re-establishing, not chasing |
| Out and down, after a fall | Close a near leg, open a lower strike further out | YES — only as the −20% row, funded by protection that gained | We are recycling a winner, not rescuing a loser |
| Down, same expiry | Move the strike toward spot because spot fell | NO | Realizes the loss, lowers the level we must recover to, and pays a spread for both |
| Up, chasing | Move the strike up because spot rose | NO | We pay to buy back what we just sold ourselves |
| In — shortening expiry | Close a far leg, open a nearer one | NO, except to close a position we want gone | Shortening time on this book is selling the one thing we are long of |
| Diagonal or calendar | Sell near, buy far, different strikes | CHRIS ONLY, and only if the package prices inside model | Most optimizer proposals land here |

**The three tests every roll has to pass. **All three, not two.

- **Does it keep the floor where it is, or higher? ****A roll that lowers the floor is a roll down wearing different clothes. No exceptions, including when the optimizer suggests it.**
- **Is it a credit, or a debit with a written reason inside the per-trade limit? ****Most legitimate rolls come back as a credit or close to flat. A roll that costs real money is telling us something is wrong with the position, not that we should pay to keep it.**
- **Does the whole package price inside our model plus tolerance? ****Priced as one package, never leg by leg. A roll quoted as two trades costs us the spread twice and hides where the money went.**
**Who does them****. **Nobody rolls alone. A roll changes the shape of the book by definition — it moves a strike or a date — so it sits outside Lucas’s mandate however small it is, and goes on the handover for Chris. The only thing Lucas does alone is the perp or forward leg that may sit alongside it.

**At the stop, we close — we do not roll. **A roll at the stop level is how a de-risk quietly becomes a decision to stay in. If we have hit the stop, the position comes off.

Example — a roll we do. It is October, we hold December ETH 3,200 calls, and Monday’s view says the move we are waiting for needs until March. The optimizer proposes rolling December to March at the same 3,200 strike for a net debit of US$90k. Test one: floor unchanged — pass. Test two: US$90k debit, inside the US$150k per-trade limit, and the reason is written down (the view needs the time) — pass. Test three: quoted as one package to two counterparties, best at US$96k against our model of US$90k — inside tolerance, pass. Chris approves it in the overlap, Lucas executes, both log it.

Example — a roll we refuse. Same book, ETH has fallen to 2,600, and the December 3,200 calls are nearly worthless. The optimizer proposes rolling them down to December 2,700 for a US$40k debit, which improves the scenario grid at today’s spot. It fails test one: it lowers the level we need to get back to and crystallizes what is left of the old position. We leave the calls to expire and, if we still believe in the view, we buy time at the same strike instead. A better-looking curve today is not a reason to give up the level.

## **Part B — Execution**

### **B1. The levels**

Monday we write the reference price and the six prices around it. During the week nobody decides anything: look at the price, find the row, do what it says.

Every row names its instrument, and the choice comes from A4. Read it literally: if a row says forward or perp, no option is traded that day. If a row says calls or puts, it is an option trade, it changes the shape of the book, and that is why those rows need Chris.

**ETH **— example uses a US$3,000 reference. Replace with Monday’s actual price.

| **If ETH trades at** | **Move** | **What we do** | **Who decides** |
| --- | --- | --- | --- |
| 3,900 or higher | +30% | Stop. Call Chris before trading anything. | Chris |
| 3,600 | +20% | Sell a quarter of our winning calls. Spend that money on puts. Move the floor up. | Lucas acts, tells Chris same day |
| 3,300 | +10% | PERP OR FORWARD — sell to bring exposure back to target. No option trade. | Lucas alone |
| 3,000 | Reference | Nothing. This is the normal state. | — |
| 2,700 | −10% | Buy back part of the forward or perp hedge. No option trade. | Lucas alone |
| 2,400 | −20% | Take profit on the puts that have gained. Spend that money on longer-dated calls. | Lucas acts, tells Chris same day |
| 2,100 or lower | −30% | Stop. Call Chris before trading anything. | Chris |
| Stop price: _____ |  | De-risk the whole book, whatever we believe. | Chris |

Example — a whole week. Monday we set the reference at 3,000 and write the rows. Tuesday ETH runs to 3,310: the +10% row fires, Lucas sells US$2.5M of perp, logs it, nothing else. Wednesday it falls back to 3,050: nothing happens, because that row already fired this week. Thursday it jumps to 3,620: the +20% row fires, Lucas sells a quarter of the in-the-money calls, buys puts with the proceeds, moves the floor to 3,200 and messages Chris the same day. Friday, 3,500: nothing. Two trades in the week — under the old way it would have been six.

**FIL **— same logic, wider steps, because FIL moves more and is harder to trade out of. Example uses a US$2.00 reference.

| **If FIL trades at** | **Move** | **What we do** | **Who decides** |
| --- | --- | --- | --- |
| 2.90 or higher | +45% | Stop. Call Chris. | Chris |
| 2.60 | +30% | Sell forward or perp to reduce exposure. Buy protection only if it prices fairly against our model. | Lucas acts, tells Chris same day |
| 2.30 | +15% | PERP OR FORWARD — sell to bring exposure back to target. | Lucas alone |
| 2.00 | Reference | Nothing. | — |
| 1.70 | −15% | Buy back part of the hedge. | Lucas alone |
| 1.40 | −30% | Take profit on protection that gained. Buy longer-dated upside only if it prices fairly. | Lucas acts, tells Chris same day |
| 1.10 or lower | −45% | Stop. Call Chris. Do not unwind option structures into a thin market. | Chris |
| Stop price: _____ |  | De-risk the whole book. | Chris |

Example. FIL falls from 2.00 to 1.68. The −15% row fires: Lucas buys back part of the perp hedge, US$1.5M of notional, inside his US$2M limit. No option is touched, because unwinding a FIL option structure into a weak market costs far more than the hedge adjustment saves.

If the price sits between two rows, do nothing. That is what the rows are for.

A row fires once per week. If the price bounces back through it, we do not trade it again until Monday resets the reference — otherwise a choppy market has us paying a spread four times for nothing.

### **B1a. What to buy and what to sell at each row**

The row tells you the direction. This tells you the trade. Strikes are written relative to spot on the day, so this does not go stale.

| **Row** | **SELL** | **BUY** | **Target net cost** | **Size** |
| --- | --- | --- | --- | --- |
| +10% | Perp or forward | — | Fees only, about 2 bps | Enough to bring net delta back to the top of the band |
| +20% | 25% of the calls now in the money, nearest expiry beyond our 90-day horizon | Put spread: long at 10% below spot, short at 25% below spot, same expiry | Zero, tolerance ±US$50k | Put spread notional sized to spend the premium received, no more |
| +30% | Nothing until Chris says | Nothing until Chris says | — | — |
| −10% | — | Buy back perp or forward | Fees only | Enough to return to mid-band |
| −20% | 30–50% of the puts that have gained, whichever leg is furthest in the money | Calls 10% above spot, expiry 3–6 months out | Zero, tolerance ±US$50k | Call notional sized to the premium received |
| −30% | Nothing until Chris says | Nothing until Chris says | — | — |
| Flat 3 weeks | Any strike more than 30% from spot with under 60 days left | — | Credit to us | All of them — they will not pay |

**The sizing rule in one line: **the buy is sized by the premium the sell produced, never the other way round. We do not decide what we want and then find the money.

Worked example — ETH +20%. Spot is 3,600 against a 3,000 reference.

- **Sell: ****25% of our December 2,800 and 3,000 calls, now deep in the money. Premium received: about US$800k.**
- **Buy: ****December put spread, long 3,240 (10% below spot), short 2,700 (25% below spot). Cost: about US$750k for the notional that US$800k supports.**
- **Net: ****US$50k credit to us. Inside tolerance, no approval needed on cost.**
- **Result: ****the floor moves from 2,700 to 3,240 and we still hold 75% of the upside.**
Worked example — ETH −20%. Spot is 2,400.

- **Sell: ****half of the December 2,700 puts, which have gained. Premium received: about US$600k.**
- **Buy: ****March calls struck 2,640 (10% above spot). Cost: about US$580k.**
- **Net: ****roughly flat.**
- **Result: ****we have converted protection that already did its job into the leg that pays if it comes back, and we did not roll a single strike down.**
**FIL is the same shape with one change: **at every row, do the linear leg first and only trade the option if it prices inside our model plus tolerance. If it does not, the perp alone is the whole trade and we accept a less perfect book rather than pay the exit.

### **B2. Who acts, and when**

Lucas works 09:00–18:00 UK. Chris works 09:00–18:00 ET, which is 14:00–23:00 UK. That gives four hours of overlap and a covered day from 09:00 UK to 23:00 UK, plus a five-minute close check at 02:00 UK before the book is left on resting orders.

**The day, hour by hour**

| **UK** | **ET** | **Who** | **What they do** |
| --- | --- | --- | --- |
| 09:00 | 04:00 | Lucas | Open. Where is spot against the rows, did any resting order fill overnight, margin and funding check. |
| 09:30 | 04:30 | Lucas | Run the optimizer. Pull today’s curve for ETH and FIL. |
| 10:00 | 05:00 | Lucas | Compare today’s curve to yesterday’s and to Monday’s view. Is there an improvement available, and is it inside mandate? |
| 10:00–15:30 | 05:00–10:30 | Lucas | Trade if a row has fired or the optimizer proposes something inside mandate. Two quotes minimum, log every trade. |
| 14:00 | 09:00 | Chris | Starts. Reads the log so far, does not trade yet. |
| 15:30 | 10:30 | Lucas | Write the handover note. |
| 16:00 | 11:00 | Both | HANDOVER. Chris re-runs the optimizer on his side and the two curves are compared. |
| 16:00–18:00 | 11:00–13:00 | Both | Overlap. Anything above Lucas’s mandate is decided and executed here, together. |
| 18:00 | 13:00 | Lucas | Off. Chris has the book. |
| 18:00–22:00 | 13:00–17:00 | Chris | US session. Trades every row, handles anything Lucas escalated. |
| 22:00 | 17:00 | Chris | Mark the book, update the scenario grid, record what dealing cost today. |
| 23:00 | 18:00 | Chris | Write tomorrow’s brief for Lucas. Place the resting orders for the night. |
| 02:00 | 21:00 | Chris | Close check, five minutes. Orders still live, no margin issue. Then off. |
| 02:00–09:00 | 21:00–04:00 | Nobody | Resting orders only. |

Clocks. The gap is normally 5 hours, but for about two weeks each spring and autumn the UK and US change clocks on different dates and it becomes 4. Anchor everything to UK time and let the ET column shift — do not re-agree the schedule twice a year.

### **B2a. The two-curve check**

The point of both of us running the optimizer is not duplication — it is a second reading, taken eight hours apart, on the same book.

|  | **Lucas, 09:30 UK** | **Chris, 11:00 ET** |
| --- | --- | --- |
| Runs | Optimizer on ETH and FIL | Same, on the US session |
| Produces | Today’s curve and the proposed improvements | His curve and his proposed improvements |
| Compares against | Yesterday’s curve, and Monday’s view | Lucas’s morning curve |

**What the comparison tells us**

| **Result** | **What it means** | **What we do** |
| --- | --- | --- |
| Both curves agree, and agree with Monday’s view | The book is behaving as expected | Act on the shared recommendation, inside mandate |
| Curves agree with each other but not with Monday’s view | The market has moved away from our thinking | Note it. Do not reshape midweek — it goes to Monday |
| Curves disagree with each other | Either the market moved hard between the two runs, or an input is wrong | Neither of us trades until we know which. Check the inputs first |
| Either curve proposes something outside mandate | Not Lucas’s call | Goes on the handover for Chris |

Example. Lucas runs at 09:30 UK; the optimizer suggests rolling the ETH December position out one month for a net credit of US$40k. Inside his mandate on size, but it changes the shape of the curve, so it is not his call — it goes on the handover. Chris re-runs at 11:00 ET: his run suggests the same roll but at a credit of US$31k, because ETH moved 1.5% in between. The curves agree, the direction is the same, the difference is just the move. They do it in the overlap at 16:00 UK, quoted to two counterparties, and log it.

Counter-example. Same morning, but Chris’s run suggests the opposite trade. That is not a market move, that is an input problem — a stale price, a wrong position, a missing leg. Nobody trades until it is found.

### **B2b. Mandate and the handover**

**Lucas acts alone on: **the ±10% ETH and ±15% FIL rows, any forward or perp trade inside the size limit, closing a position where the loss is already taken in substance, and refusing a quote.

**Lucas puts on the handover instead: **the ±30% ETH and ±45% FIL rows, anything that changes the shape of the curve rather than the direction, anything costing more out of pocket than the per-trade limit, any increase in size, and anything the optimizer proposes that contradicts Monday’s view.

**The handover at 16:00 UK / 11:00 ET. **Written, not spoken, same six lines every day:

- **Spot for ETH and FIL, and distance to the next row**
- **What the optimizer said this morning, and what we did or did not do about it**
- **Anything traded since the last handover, with what it cost against our model**
- **Anything waiting for Chris, and why**
- **Margin headroom and funding paid**
- **Anything that would change Monday’s view**
**The night orders. **At 23:00 UK / 18:00 ET Chris places them: level, size, direction, pre-agreed. They are cancelled and rewritten at the next handover so nothing stale sits in the market. The 02:00 UK close check confirms they are still live.

Example — the overnight. Chris signs off at 23:00 UK with ETH at 3,050 and leaves: sell US$2.5M perp if ETH trades 3,300. At 02:00 UK he checks — order live, margin fine, nothing to do. At 03:40 ETH spikes to 3,340 and the order fills at 3,305. Lucas opens at 09:00 UK, sees the fill in the log, confirms exposure is back at target, and does not trade that row again this week.

**Wake the other one up for: **a move past the ±30% ETH or ±45% FIL row, a margin call, or a counterparty refusing to quote. Nothing else.

### **B3. How we deal**

Four habits. None of them require a decision.

- **Price it ourselves first. ****We have our own pricing models for ETH and FIL. Run the structure through them before asking anybody. If the quote is wider than our number plus the agreed tolerance, we do not trade.**
- **Two quotes minimum. ****Same legs, same size, same moment, with a deadline for the answer. Three is better when the structure is vanilla enough that people will quote it, but on anything bespoke asking three tends to leak the trade and widen the price rather than tighten it. Two is the floor, and one is not a price.**
- **Quote the whole package, never the legs. ****Ask for the full structure as one price. Unwinding one position and opening another as two separate trades means paying the spread twice.**
- **Log every trade the same way: ****Our model price, the best quote, the cover quote, what we actually paid, and the difference in dollars. Five fields. Without them we cannot see what dealing is costing us, and what we cannot see we keep paying.**
Example — what a log line looks like.

| **Date** | **Trade** | **Our model** | **Best quote** | **Cover** | **Paid** | **vs model** |
| --- | --- | --- | --- | --- | --- | --- |
| 24 Sep | ETH 3,200 put spread, 5,000 lots | 612,000 | 638,000 | 661,000 | 638,000 | 26,000 |

One line, seven fields, filled in at the time of the trade. Twelve of these in a month and we can see exactly what dealing costs us — in that example, US$26k on one trade, four times what the whole month’s perp funding costs.

**Monthly, twenty minutes: **total dealing cost, total time-decay cost, funding paid on perps, and a ranking of which counterparty priced best. That page is the only evidence of whether any of this is working.

## **What we agree now**

Split the same way, so it is clear what needs a conversation and what just needs filling in.

**Strategy — needs both of us, one call**

- **The view: do we believe in a big move in the next three months? Everything else follows from this.**
- **If the answer is no, what the book should be reshaped into — and that is a project, not an adjustment.**
- **The stop price for each asset.**
- **The roll policy in A5: confirmed that we stop moving strikes down, and that no roll happens without Chris.**
**Execution — Chris sets, Lucas fills in**

- **Lucas’s size limit and cost limit.**
- **Where forwards and perps trade, and how much collateral sits there.**
- **Monday’s reference prices, which populate every row in B1.**
- **The handoff time, confirmed — 15:00 UK works if Chris starts at 09:00 ET.**
Once those are answered, nothing in Part B requires a phone call, and that is the test of whether the split is real.

Internal working framework, not investment advice.
