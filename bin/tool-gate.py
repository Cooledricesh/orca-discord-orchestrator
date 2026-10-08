#!/usr/bin/env python3
# 툴 게이트는 제거됨. 제거 전에 뜬 마크 세션이 아직 이 훅을 부르므로 아무것도 하지 않고 통과시킨다
# (파일이 없으면 python 이 exit 2 로 끝나 PreToolUse 에서 Bash 가 막힌다). 그 세션들이 끝나면 지운다.
