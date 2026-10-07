# CLAUDE.md

공통 지침은 AGENTS.md 한 곳에서 관리한다. 내용을 여기에 복사하지 않는다.

@AGENTS.md

## Claude Code 전용

- 같은 cmux 워크스페이스의 다른 창(Codex, Antigravity)은 `ListAgents`에 나오지 않는다.
  `cmux tree` 로 창을 확인하고 `cmux paste --surface <ref> --submit "<요청>"` 으로 요청을 보낸다.
  `ListAgents`에 보이는 세션은 다른 워크스페이스·다른 프로젝트일 수 있으니, 보내기 전에 대상을 확인한다.
- 다른 에이전트에게 맡긴 분석 결과는 세션 scratchpad 아래 파일로 받는다(저장소에 분석 문서를 만들지 않는다).
