# Reference-photo classifier (2026-09-26)

Trained on 1002 degraded crops from underwater reference photos of 50 species, plus 1360 crops of this camera's background as "not an animal". Species with no underwater reference photo are left to zero-shot.

**Test** on 230 crops of held-out underwater photos (simulated camera conditions), and on 680 background crops from frames the classifier didn't train on. The classifier's cutoff is set so its false alarms on the background match the tracker's.

| | Names only (now) | With the reference classifier |
|---|---:|---:|
| Cutoff | 0.85 | 0.50 |
| False alarms on this camera's background | 17.4% | 0.0% |
| Fish named | 55.1% | 65.9% |
| Right, of those named | 89.8% | 91.5% |
| Right, of all | 49.5% | 60.3% |
| Young blacksmith: right / confidently wrong (of 16) | 4 / 2 | 7 / 3 |

Runs in **shadow mode**: see `evaluate` below for how it does on real review answers.

Species it knows: California moray eel, California scorpionfish, California sea hare, California sea lion, California sheephead, California spiny lobster, Pacific barracuda, Pacific sea nettle jellyfish, barred sand bass, bat ray, bat star, black perch, black sea nettle jellyfish, blacksmith, bullseye pufferfish, cabezon, diamond stingray, finescale triggerfish, garibaldi, giant kelpfish, giant spined sea star, green sea turtle, halfmoon, harbor seal, horn shark, jack mackerel, kelp bass, leopard shark, market squid, moon jellyfish, northern anchovy, octopus, opaleye, painted greenling, pelagic red crab, pile perch, purple-striped jellyfish, rainbow surfperch, rock wrasse, round stingray, rubberlip seaperch, salema, sargo, senorita, sheep crab, shiner perch, topsmelt, yellow rock crab, yellowtail amberjack, zebra-perch sea chub

## On people's review answers (2026-10-05)

1 answered pictures with a shadow opinion. Not enough answers yet: 1 of the 30 needed.

| | Tracker (names only) | Reference classifier |
|---|---:|---:|
| Right | 1/1 (100%) | 0/1 (0%) |
| Right, when it was an animal (1) | 1 (100%) | 0 (0%) |

