import test from "node:test";
import assert from "node:assert/strict";

import {
  buildQuestionnaireAnswer,
  clearQuestionChoice,
  createQuestionnaireDraft,
  isQuestionChoiceSelected,
  toggleQuestionChoice,
  validateQuestionnaire,
} from "./questionnaireState.js";

const questions = [
  {
    id: "direction",
    type: "choice",
    question: "方向？",
    required: true,
    multiple: false,
    options: [
      {label: "方案 A", value: "a", description: "稳妥"},
      {label: "方案 B", value: "b", description: "激进"},
    ],
    recommendation: {values: ["b"], reason: "仅作提示"},
  },
  {id: "constraints", type: "open", question: "限制？", required: false},
];

test("draft starts empty and recommendation never preselects", () => {
  const draft = createQuestionnaireDraft(questions);
  assert.deepEqual(draft, {answers: {
    direction: {selectedValues: [], text: ""},
    constraints: {selectedValues: [], text: ""},
  }});
  assert.equal(isQuestionChoiceSelected(draft, questions[0], questions[0].options[1]), false);
});

test("single choice can be changed and explicitly cleared", () => {
  const draft = createQuestionnaireDraft(questions);
  toggleQuestionChoice(draft, questions[0], "a");
  toggleQuestionChoice(draft, questions[0], "b");
  assert.deepEqual(draft.answers.direction.selectedValues, ["b"]);
  clearQuestionChoice(draft, questions[0]);
  assert.deepEqual(draft.answers.direction.selectedValues, []);
});

test("multiple choice toggles independently", () => {
  const multi = {...questions[0], multiple: true};
  const draft = createQuestionnaireDraft([multi]);
  toggleQuestionChoice(draft, multi, "a");
  toggleQuestionChoice(draft, multi, "b");
  toggleQuestionChoice(draft, multi, "a");
  assert.deepEqual(draft.answers.direction.selectedValues, ["b"]);
});

test("required choice accepts text-only and preserves original text", () => {
  const draft = createQuestionnaireDraft(questions);
  draft.answers.direction.text = "  我需要未列出的方案，保留换行\n与细节  ";
  assert.deepEqual(validateQuestionnaire(questions, draft), {});
  const payload = buildQuestionnaireAnswer(questions, draft);
  assert.deepEqual(Object.keys(payload).sort(), ["answers", "cancelled"]);
  assert.deepEqual(payload.answers[0], {
    questionId: "direction",
    selectedValues: [],
    text: "  我需要未列出的方案，保留换行\n与细节  ",
  });
  assert.deepEqual(Object.keys(payload.answers[0]).sort(), ["questionId", "selectedValues", "text"]);
});

test("required validation and ordered options-with-text payload", () => {
  const draft = createQuestionnaireDraft(questions);
  assert.ok(validateQuestionnaire(questions, draft).direction);
  toggleQuestionChoice(draft, questions[0], "a");
  draft.answers.direction.text = "补充限制";
  draft.answers.constraints.text = "开放答案";
  assert.deepEqual(buildQuestionnaireAnswer(questions, draft), {
    cancelled: false,
    answers: [
      {questionId: "direction", selectedValues: ["a"], text: "补充限制"},
      {questionId: "constraints", selectedValues: [], text: "开放答案"},
    ],
  });
});

test("cancel payload contains no draft answers", () => {
  const draft = createQuestionnaireDraft(questions);
  draft.answers.direction.text = "不得外发的草稿";
  assert.deepEqual(buildQuestionnaireAnswer(questions, draft, true), {cancelled: true});
});
