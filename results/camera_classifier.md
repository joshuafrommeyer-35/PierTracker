# Camera classifier (2026-10-08 22:00)

80 answered pictures with embeddings. An animal is learned once it has 12 answers; ~30 makes it reliable.

| Animal | Answers | Progress |
|---|---:|---|
| not an animal | 43 | `##########` ready |
| blacksmith | 16 | `#####     ` ready |
| kelp bass | 13 | `####      ` ready |
| California spiny lobster | 4 | `#         `  |
| salema | 2 | `          `  |
| barred sand bass | 1 | `          `  |
| jacksmelt | 1 | `          `  |

Trained on 72 answers, 3 animals, 5-fold cross-validation:

- camera-trained classifier: **99%** right
- zero-shot BioCLIP on the same pictures: **29%** right
- **switched on**: the tracker now uses it for these animals.

```
               precision    recall  f1-score   support

   blacksmith       1.00      1.00      1.00        16
    kelp bass       1.00      0.92      0.96        13
not an animal       0.98      1.00      0.99        43

     accuracy                           0.99        72
    macro avg       0.99      0.97      0.98        72
 weighted avg       0.99      0.99      0.99        72

```
