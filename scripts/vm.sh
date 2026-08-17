#!/usr/bin/env bash
# resource_web 版本管理（基于 Git tag，替代 versions/ 手工拷贝）
# 用法：
#   ./scripts/vm.sh status          # 当前版本 / 脏文件
#   ./scripts/vm.sh list            # 列出版本标签
#   ./scripts/vm.sh snap [说明]     # 打标签快照（需工作区干净或 --force）
#   ./scripts/vm.sh show <tag>      # 查看某版本说明
#   ./scripts/vm.sh checkout <tag>  # 检出到某版本（脱离 main，只读查看）
#   ./scripts/vm.sh restore <tag>   # 恢复某版本到工作区（需确认）
#   ./scripts/vm.sh bump patch|minor|major [说明]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

git_c() {
  git -c safe.directory="$ROOT" "$@"
}

VERSION_FILE="$ROOT/VERSION"
PREFIX="v"

current_version() {
  if [[ -f "$VERSION_FILE" ]]; then
    tr -d '[:space:]' <"$VERSION_FILE"
  else
    echo "0.0.0"
  fi
}

ensure_git() {
  if ! git_c rev-parse --git-dir >/dev/null 2>&1; then
    echo "错误: 不是 git 仓库: $ROOT" >&2
    exit 1
  fi
}

cmd_status() {
  ensure_git
  local ver branch dirty
  ver="$(current_version)"
  branch="$(git_c rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  echo "VERSION: $ver"
  echo "BRANCH:  $branch"
  echo "HEAD:    $(git_c rev-parse --short HEAD)"
  if git_c status --porcelain | grep -q .; then
    echo "DIRTY:   yes（有未提交改动）"
    git_c status -sb
  else
    echo "DIRTY:   no"
  fi
  echo "TAGS:    $(git_c tag -l "${PREFIX}*" | wc -l | tr -d ' ') 个版本标签"
}

cmd_list() {
  ensure_git
  if ! git_c tag -l "${PREFIX}*" | grep -q .; then
    echo "（还没有版本标签，用: ./scripts/vm.sh snap \"说明\"）"
    return 0
  fi
  git_c for-each-ref --sort=-creatordate \
    --format='%(refname:short)  %(creatordate:short)  %(subject)' \
    "refs/tags/${PREFIX}*"
}

cmd_show() {
  local tag="${1:-}"
  [[ -n "$tag" ]] || { echo "用法: $0 show <tag>"; exit 1; }
  ensure_git
  git_c show --no-patch --format=fuller "$tag"
}

cmd_snap() {
  ensure_git
  local note="${*:-snapshot}"
  local force=0
  if [[ "${1:-}" == "--force" ]]; then
    force=1
    shift || true
    note="${*:-snapshot}"
  fi
  local ver tag
  ver="$(current_version)"
  tag="${PREFIX}${ver}"

  if git_c rev-parse "$tag" >/dev/null 2>&1; then
    echo "标签已存在: $tag"
    echo "请先 bump 版本: ./scripts/vm.sh bump patch \"说明\""
    exit 1
  fi

  if git_c status --porcelain | grep -q .; then
    if [[ "$force" -ne 1 ]]; then
      echo "工作区有未提交改动。请先提交，或："
      echo "  1) git add/commit 后再 snap"
      echo "  2) ./scripts/vm.sh snap --force \"说明\"  （仅打在当前 HEAD，不含未提交文件）"
      exit 1
    fi
    echo "警告: --force，标签打在当前 HEAD，未提交文件不会进该版本"
  fi

  git_c tag -a "$tag" -m "$note"
  echo "已打标签: $tag"
  echo "说明: $note"
  echo "查看: ./scripts/vm.sh show $tag"
  echo "推送远端(可选): git push origin $tag"
}

bump_semver() {
  local ver="$1" part="$2"
  local major minor patch
  ver="${ver%%-*}" # strip -dev suffix
  IFS=. read -r major minor patch <<<"$ver"
  major=${major:-0}; minor=${minor:-0}; patch=${patch:-0}
  case "$part" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
    *) echo "未知级别: $part (用 patch|minor|major)"; exit 1 ;;
  esac
  echo "${major}.${minor}.${patch}"
}

cmd_bump() {
  ensure_git
  local part="${1:-}"
  shift || true
  local note="${*:-}"
  [[ -n "$part" ]] || { echo "用法: $0 bump patch|minor|major [说明]"; exit 1; }
  local old new
  old="$(current_version)"
  new="$(bump_semver "$old" "$part")"
  echo "$new" >"$VERSION_FILE"
  echo "VERSION: $old -> $new"
  if [[ -n "$note" ]]; then
    cmd_snap "$note"
  else
    echo "已写入 VERSION=$new。提交后执行: ./scripts/vm.sh snap \"说明\""
  fi
}

cmd_checkout() {
  local tag="${1:-}"
  [[ -n "$tag" ]] || { echo "用法: $0 checkout <tag>"; exit 1; }
  ensure_git
  echo "将进入分离 HEAD 查看 $tag（改完请切回 main）"
  git_c checkout "$tag"
  echo "返回主线: git checkout main"
}

cmd_restore() {
  local tag="${1:-}"
  [[ -n "$tag" ]] || { echo "用法: $0 restore <tag>"; exit 1; }
  ensure_git
  echo "将把工作区恢复为 $tag 的内容（会覆盖未提交改动！）"
  read -r -p "确认恢复？输入 yes: " ans
  [[ "$ans" == "yes" ]] || { echo "已取消"; exit 1; }
  git_c checkout "$tag" -- .
  # 同步 VERSION 文件若标签名是 vX.Y.Z
  if [[ "$tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}" >"$VERSION_FILE"
  fi
  echo "已检出文件到工作区。请检查后自行 commit。"
  git_c status -sb
}

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \?//'
}

main() {
  local cmd="${1:-status}"
  shift || true
  case "$cmd" in
    status|st) cmd_status "$@" ;;
    list|ls) cmd_list "$@" ;;
    show) cmd_show "$@" ;;
    snap|tag) cmd_snap "$@" ;;
    bump) cmd_bump "$@" ;;
    checkout|co) cmd_checkout "$@" ;;
    restore) cmd_restore "$@" ;;
    -h|--help|help) usage ;;
    *) echo "未知命令: $cmd"; usage; exit 1 ;;
  esac
}

main "$@"
