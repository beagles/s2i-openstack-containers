# Sourced repeatedly by DIB, including before bootloader finalisation.
# Preserve the ordinary boot arguments, but make guest SELinux unambiguously off.
read -r -a _amphora_cmdline <<< "${DIB_BOOTLOADER_DEFAULT_CMDLINE:-nofb nomodeset gfxpayload=text}"
_amphora_args=()
for _amphora_arg in "${_amphora_cmdline[@]}"; do
    case ${_amphora_arg} in
        selinux=*) ;;
        *) _amphora_args+=("${_amphora_arg}") ;;
    esac
done
_amphora_args+=(selinux=0)
export DIB_BOOTLOADER_DEFAULT_CMDLINE="${_amphora_args[*]}"
unset _amphora_cmdline _amphora_args _amphora_arg
