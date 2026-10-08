## Summary

Explain what the PR changes and why.

## Testing

Commands used to test locally (copy/paste runnable):

```bash
# Example
bash install.sh --check /root/answers.conf
systemctl restart element-stack.service
docker compose -f /opt/element-stack/compose.json ps
```

## Compatibility / Rollback

Any breaking changes? How to roll back safely?
