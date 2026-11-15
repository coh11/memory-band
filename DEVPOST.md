# Memory Band: Devpost submission

**Tagline:** The wristband that notices what you forgot, without you lifting a finger.

**Links**
- Code: https://github.com/coh11/memory-band
- Demo video: _add link_

---

## Inspiration

"Did I lock the door? Did I take my pill? Where did I leave my phone?"

For millions of older adults with early memory loss, small everyday things turn into constant worry. They leave the front door unlocked, take the morning pill twice (or not at all), leave the phone on a café table, or leave the stove on. Families end up being the memory: they call several times a day just to check.

Most reminder apps only work if you remember to use the app, and that's exactly what someone who is forgetting can't do. We wanted a memory aid that asks **nothing** of the wearer: no buttons, no app, nothing to say. The band on the wrist simply notices.

## What it does

Memory Band turns the Vexo Open band into a **passive** memory assistant. It listens for everyday sounds and feels everyday movements: locking the door, opening a pill bottle, putting the phone down. When something gets missed, a push notification goes to the wearer's phone. If it still isn't fixed, the family gets one too. When the band notices the fix, the alert closes by itself and a ✅ follow-up goes out.

| Use case | How the band knows | What happens |
|---|---|---|
| **Forgot to lock the door** | Leaving home is detected by the phone leaving Home (iPhone Shortcut) or the band's Bluetooth signal fading. Locking is detected by the lock's click (mic) or the key-turn twist of the wrist (motion). | No lock heard or felt → *"You didn't lock the door"* about 20 s after leaving. The family is told 10 min later. Lock it → ✅ *"Front door locked at 3:42 PM, thanks! (40 s after the alert)"* |
| **Pills** | Pill-bottle rattle (mic) plus a cap twist or hand to mouth (motion), within 30 s | A reminder at dose time and a follow-up if nothing's noticed. The family is told after 60 min. Taking it twice → *"Possible double dose"* to wearer and family |
| **Phone left behind** | Phone set down (a clack plus the hand going down and resting), then walking away (the band's step counter) with no pick-up | *"Don't forget your phone"* within about 5 s of walking off. Picking it up resolves it |
| **Fall** | Free-fall on the band | A 30 s countdown that a double-tap cancels; otherwise an urgent alert to the family |
| **Stove** | A daily evening check | Answered with a double-tap; an alert if it's left on |
| **Double-tap = "yes, I did it"** | Band tap | Answers the latest question: pill taken, door locked, stove off, "I'm OK" |

**Built for an older person:**
- **No app to install for the wearer.** Notifications use the free ntfy app, with no account.
- **A step-by-step setup page in the browser** records *your own* door lock, key turn, pill bottle and phone set-down (3 samples each), then runs a live "lock the door once" test.
- **A family dashboard** shows open alerts, resolved alerts and how long each took to fix.

**Optional speech extra:** with Deepgram on, the wearer can also say things like "dentist Tuesday at 3" (it becomes a reminder), "I put my keys in the drawer", or "this is my neighbor Tom". They can ask "where are my keys?" and the answer comes on the phone. If the same question comes up 3 times in 30 minutes, the family gets a gentle note.

## How we built it

- **Band → home laptop:** a Python listener connects to the Vexo Open band over Bluetooth (BLE) through the Vexo Open Kit. It receives Opus audio from the mic, gyro and accelerometer data, the step counter, taps and free-fall events, and the Bluetooth signal strength.
- **Sound detection (NumPy only):** an onset detector catches short sounds. A lock click or set-down clack is matched with a 24-band spectrogram; the pill rattle is matched with a 1-second "texture" (average spectrum plus click count). Each is compared by cosine similarity against the wearer's own 3 calibration samples. We left out large models like YAMNet: there's no "pill bottle" class, and three samples of *your* lock beat a generic model.
- **Motion detection:**
  - **Key turns and cap twists:** gyro rotation within an angle window (70–200° for a key).
  - **Up/down moves:** we find which way is up from gravity, rotate it with the gyro during the move, and integrate twice to tell set-down from pick-up from hand to mouth.
  - **Walking:** the band's own step counter.
- **Leaving home:** an iPhone Shortcuts automation calls the server when the phone leaves Home (through a tunnel protected by a token). A fading Bluetooth signal between band and laptop is the fallback.
- **Server:** FastAPI plus SQLite. Every event goes into a timeline, and a rules engine runs every 5 seconds. Each alert moves through **open → push sent → escalated or resolved**, with the time it took to fix. Notifications go out through ntfy push, one topic for the wearer and one for the family.
- **Speech (optional):** Deepgram turns speech into text, and a tagger (Gemini, then OpenAI, falling back to offline keywords) turns sentences into events.
- **Testing without hardware:** an offline test suite feeds synthetic signals to every detector, including the things that must *not* count: knocks, voices, hum, arm moves at the table. A pretend-band mode and a "whole day in 1 minute" replay script exercise every rule.

## Challenges we ran into

- **The band has no speaker, vibration or usable light,** since the LED is locked to solid blue while the mic is on. Every piece of feedback has to go through the phone, so we built the setup page to show calibration progress live.
- **Telling sounds apart on a tiny wrist mic:** a lock click versus a knock, a pill rattle versus a voice, a phone set-down versus a cup. Calibrating on the wearer's own lock and pill bottle made the difference.
- **Movements that look alike:**
  - Standing up to walk away looks like picking the phone back up. We wait 4 s to see if walking starts.
  - Closing the pill cap looks like a second dose. We treat repeats within 60 s as the same dose.
  - A key turn and a cap twist are both wrist rotations.
- **Knowing someone has left home** without GPS on the band: we combined a phone geofence with the band's Bluetooth signal fading as a fallback.
- **Making it demo-proof:** pretend-band scripts, a demo clock and a dry-run mode for notifications let us test every flow without the band or a real phone.

## Accomplishments that we're proud of

- **It's truly passive.** The wearer never has to say or press anything, and alerts close by themselves when the band notices the fix.
- **It works from wrist to phone:** band → Bluetooth → detection → rules → a push notification, with escalation to family.
- **Personal calibration in about 3 minutes** from a simple browser page, using NumPy only, with no model downloads.
- **An offline test suite** for every detector, including the false positives we had to avoid.
- **It measures what matters:** every alert records how long it took to fix, so families see the outcome, not just the warning.

## What we learned

- With only a few samples, simple signal processing tuned to one person beats a big generic model.
- For memory support, **timing is everything**: a reminder 20 seconds after you leave is useful, one an hour later is just stress.
- Combining two weak signals (a click *and* a twist, a rattle *and* hand to mouth) is far more reliable than trusting either one alone.
- Designing for older adults means removing steps: no app install, a double-tap as the only input, and family alerts that stay quiet until they're needed.

## What's next for Memory Band

- **Leaving checklist:** one combined check as you walk out (door, phone, keys, stove).
- **Stove left on, detected by sound,** replacing the daily check.
- **A daily summary for family:** "pills ✅✅, door locked every time, no falls."
- **Learning routines,** so reminders adapt to when the wearer usually takes their pills.
- **A health report for doctor visits:** pill adherence, falls, and repeated questions over time.
- **Testing with older adults and caregivers.**

## Built with

`vexo-open-band` · `python` · `fastapi` · `sqlite` · `numpy` · `bleak` · `bluetooth-le` · `ntfy` · `ios-shortcuts` · `html` · `javascript` · `deepgram` · `gemini` · `openai`

_(Drop `deepgram`, `gemini` and `openai` if speech isn't part of your demo.)_

## Team

- Charles (Younghoon) Oh: [@coh11](https://github.com/coh11)
- Mamoune: [@melboukfaoui](https://github.com/melboukfaoui)
