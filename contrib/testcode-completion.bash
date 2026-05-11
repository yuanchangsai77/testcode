_testcode_completions()
{
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    local target="${COMP_WORDS[0]}"

    local opts="--once --list --resume --last --help"

    if [[ "${target}" == "python3" ]]; then
        if [[ "${#COMP_WORDS[@]}" -lt 3 || "${COMP_WORDS[1]}" != "-m" || "${COMP_WORDS[2]}" != "testcode" ]]; then
            return
        fi
    fi

    if [[ "${prev}" == "--resume" ]]; then
        local session_dir
        session_dir="$(pwd)/.testcode/sessions"
        if [[ -d "${session_dir}" ]]; then
            local sessions
            sessions="$(cd "${session_dir}" 2>/dev/null && printf '%s\n' *.json | sed 's/\.json$//' | grep -v '^\*$')"
            COMPREPLY=( $(compgen -W "${sessions}" -- "${cur}") )
        fi
        return
    fi

    COMPREPLY=( $(compgen -W "${opts}" -- "${cur}") )
}

complete -F _testcode_completions testcode
complete -F _testcode_completions python3
