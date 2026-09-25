# Reference-photo classifier (2026-09-25)

Trained on 970 degraded crops from underwater reference photos of 48 species, plus 1360 crops of this camera's background as "not an animal". Species with no underwater reference photo are left to zero-shot.

**Test** on 216 crops of held-out underwater photos (simulated camera conditions), and on 680 background crops from frames the classifier didn't train on. The classifier's cutoff is set so its false alarms on the background match the tracker's.

| | Names only (now) | With the reference classifier |
|---|---:|---:|
| Cutoff | 0.85 | 0.50 |
| False alarms on this camera's background | 17.8% | 0.0% |
| Fish named | 54.0% | 65.5% |
| Right, of those named | 90.7% | 92.4% |
| Right, of all | 49.0% | 60.5% |
| Young blacksmith: right / confidently wrong (of 16) | 4 / 2 | 7 / 3 |

Runs in **shadow mode**: see `evaluate` below for how it does on real review answers.

Species it knows: California moray eel, California scorpionfish, California sea hare, California sea lion, California sheephead, California spiny lobster, Pacific barracuda, Pacific sea nettle jellyfish, barred sand bass, bat ray, bat star, black perch, black sea nettle jellyfish, blacksmith, bullseye pufferfish, cabezon, diamond stingray, finescale triggerfish, garibaldi, giant kelpfish, giant spined sea star, green sea turtle, halfmoon, harbor seal, horn shark, jack mackerel, kelp bass, leopard shark, market squid, moon jellyfish, northern anchovy, octopus, opaleye, painted greenling, pelagic red crab, pile perch, purple-striped jellyfish, round stingray, rubberlip seaperch, salema, sargo, senorita, sheep crab, shiner perch, topsmelt, yellow rock crab, yellowtail amberjack, zebra-perch sea chub

## On people's review answers (2026-09-25)

0 answered pictures with a shadow opinion. Not enough answers yet: 0 of the 30 needed.

