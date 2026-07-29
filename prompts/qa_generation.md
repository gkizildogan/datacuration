You create exactly one evidence-grounded aviation question from the supplied
anchor. Return only JSON conforming to the task-specific response schema.

The `anchor_id` identifies the exact evidence span. Never quote or rewrite the
anchor as evidence: the pipeline constructs evidence from its stored offsets.
Use only facts explicitly present in `anchor`.

Universal rules:

- Write the question in `question_language`.
- A cross-lingual task changes only the question language. Answers stay exactly
  as written in the anchor; never translate an answer span.
- Do not calculate, infer, convert units, combine non-contiguous facts, reorder
  list items, or use outside knowledge.
- Do not put the answer verbatim in the question.
- When `required_question_term` is non-null, include that exact term naturally
  in the question. It will later support a deterministic unanswerable mutation.
- If the anchor cannot support the requested type without violating a rule,
  return `{"kind":"reject","reject_reason":"..."}`.

Closed-answer contracts:

- `factual`: return exactly one `answer_items` entry. It must be an exact,
  contiguous substring of the anchor.
- `temporal`: return exactly one `answer_items` entry containing an explicit
  date, year, time, or duration copied exactly from the anchor.
- `list_table`: return one or more exact anchor substrings in source order.
  Do not merge, translate, sort, or normalize the items.

Explanatory contracts:

- `definition`: return a concise `reference_answer` grounded entirely in the
  anchor and a non-empty `rubric` of required points.
- `comparison`: the anchor must explicitly relate two entities or parallel
  values. Return a grounded `reference_answer` and a non-empty `rubric`.
- Do not return `answer_items` for explanatory tasks.

Factual examples:

- English anchor: `The APU supplies electrical power on the ground.`
  Output: `{"kind":"answer","question":"What does the APU supply on the ground?","answer_items":["electrical power"]}`
- Turkish anchor: `Uçak 180 yolcu kapasitesine sahiptir.`
  Output: `{"kind":"answer","question":"Uçağın yolcu kapasitesi kaçtır?","answer_items":["180 yolcu"]}`

Definition examples:

- English anchor: `A taxiway is a defined path for aircraft movement on an aerodrome.`
  Output: `{"kind":"answer","question":"How is a taxiway described?","reference_answer":"It is a defined path for aircraft movement on an aerodrome.","rubric":["defined path","aircraft movement","on an aerodrome"]}`
- Turkish anchor: `Apron, uçakların park ettiği ve hizmet aldığı alandır.`
  Output: `{"kind":"answer","question":"Apron nasıl tanımlanır?","reference_answer":"Apron, uçakların park ettiği ve hizmet aldığı alandır.","rubric":["uçakların park etmesi","hizmet alması"]}`

List/table examples:

- English anchor: `- Runway inspection\n- Lighting check\n- Wildlife patrol`
  Output: `{"kind":"answer","question":"Which activities are listed?","answer_items":["Runway inspection","Lighting check","Wildlife patrol"]}`
- Turkish anchor: `| Kod | Meydan |\n| LTAC | Ankara |\n| LTFM | İstanbul |`
  Output: `{"kind":"answer","question":"Tabloda hangi meydanlar yer alır?","answer_items":["Ankara","İstanbul"]}`

Comparison examples:

- English anchor: `The A320 carries 180 passengers, whereas the A319 carries 156.`
  Output: `{"kind":"answer","question":"How do the stated passenger capacities differ?","reference_answer":"The A320 carries 180 passengers and the A319 carries 156.","rubric":["A320: 180","A319: 156"]}`
- Turkish anchor: `İç hat terminali 20 kapıya, dış hat terminali ise 30 kapıya sahiptir.`
  Output: `{"kind":"answer","question":"Terminallerin kapı sayıları nasıl karşılaştırılır?","reference_answer":"İç hat terminalinde 20, dış hat terminalinde 30 kapı vardır.","rubric":["iç hat: 20","dış hat: 30"]}`

Temporal examples:

- English anchor: `The airport opened on 29 October 2018.`
  Output: `{"kind":"answer","question":"When did the airport open?","answer_items":["29 October 2018"]}`
- Turkish anchor: `İlk uçuş 15 Mart 2024 tarihinde yapıldı.`
  Output: `{"kind":"answer","question":"İlk uçuş ne zaman yapıldı?","answer_items":["15 Mart 2024"]}`

Cross-lingual examples:

- Turkish anchor, English question: `Pist uzunluğu 3.000 metredir.`
  Output: `{"kind":"answer","question":"What runway length is stated?","answer_items":["3.000 metre"]}`
- English anchor, Turkish question: `The inspection interval is 100 hours.`
  Output: `{"kind":"answer","question":"Belirtilen denetim aralığı nedir?","answer_items":["100 hours"]}`

Valid rejection examples:

- A definition request over `| Code | LTFM |` must reject because the anchor
  does not define or describe a concept.
- A temporal request over `The aircraft uses two engines.` must reject because
  no explicit time value is present.
- A comparison request over a sentence about only one entity must reject.
- A list/table request over a single isolated value must reject.
