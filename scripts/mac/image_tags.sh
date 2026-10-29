#!/bin/sh
# Docker Hub에서 최근 태그를 본다 (버전 고정용).
curl -s "https://hub.docker.com/v2/repositories/qdrant/qdrant/tags?page_size=40&ordering=last_updated" | grep -o '"name":"v[0-9.]*"' | head -6
curl -s "https://hub.docker.com/v2/repositories/library/postgres/tags?page_size=60&name=17" | grep -o '"name":"17\.[0-9]*-alpine"' | head -4
curl -s "https://hub.docker.com/v2/repositories/library/python/tags?page_size=60&name=3.11" | grep -o '"name":"3\.11\.[0-9]*-slim-bookworm"' | head -4
