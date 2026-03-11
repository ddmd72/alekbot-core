

  




---

## Table of Contents









---







```python







{
    "success": true,
    "user": {
        
        "email": "user@example.com",
        
    },
    "account": {
        
        "tier": "free",
        "role": "owner"
    }
}
```









```python
from src.services.authentication_service import AuthenticationService


await auth_service.link_platform_identity(
    user_id="user_123",
    platform="slack",
    platform_user_id="U123456"
)
```



---







```python
from src.ports.iam_port import IAMPort




    user_id="user_123",
    account_id="account_456"
)

```





```python



    user_id="user_123",
    resource_type=ResourceType.FACT,
    action=Action.WRITE,
    
)


    
```





```python


    account_id="account_456",
    
    
)


# {


# }
```





```python

    account_id="account_456",
    
)
```



---







```python
from src.domain.user import UserBotConfig, PerformanceTier


account.account_defaults = UserBotConfig(
    default_tier=PerformanceTier.PERFORMANCE,
    
)

```





```python


await user_repo.update_user(user)
```





```python
from src.services.configuration_service import ConfigurationService

config_service = ConfigurationService()



    base=account.account_defaults,
    
)



```

---







```python





await auth_service.link_platform_identity(
    user_id="user_123",
    platform="telegram",
    
)
```





```python

    user_id="user_123",
    platform="telegram"
)
```

---











```python
from src.services.session_service import SessionService




```









```python
POST /auth/refresh


Response:

```





```python
POST /auth/logout

Response:


```

---







```bash








```





```bash

```




| :---------------------- | :------------------------- | :------------------------------------------ |






---

## 7. Code Reference




| :----------------------- | :--------------------------- |







| :--------------------------------------- | :--------------------------- |







| :------------------------------------------ | :---------------------- |







| :-------------------------------------------------- | :---------------- |



---

## Related Documentation


- [MIGRATION_GUIDE.md](../05_building_blocks/oauth_multi_tenant/MIGRATION_GUIDE.md) — Migration Steps
- [MULTI_TENANT_OAUTH_RFC.md](../10_rfcs/MULTI_TENANT_OAUTH_RFC.md) — Original RFC

  
**Status:** ✅ Production Ready
