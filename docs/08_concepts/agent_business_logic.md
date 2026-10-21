










```

    ↓

    ↓

    ↓

    ├─ Simple Query → QuickResponseAgent
    
    
    └─ Complex Query → SmartResponseAgent
            
            
            
            
            
    ↓

    ↓

```










**Flow:**
```




```






---










```




   
   

```

**Example:**
```
User: "Hi"
  ↓

  ↓

```










---








```



   


   

   Turn N:
     
        
     
     
           
           
     

   
   
```




  



```json
{
  
  
  
}
```


```

  ↓

  ↓

```


```

  ↓

  ↓

  ↓

```









---








```

   


   
   
   

```

**Example:**
```

  ↓

  ↓

```



  






---








```

   
   
   



   
   
   

   
   
   
```


```


```

**Example:**
```

  ↓

  ↓

  

  

    
    
  )
  
```








---











```





```

**Example:**
```

  ↓

  ↓

  ↓

```








---











```





```

**Example:**
```

  ↓

  ↓

  ↓

```








---












```

  
  
  
  


  
  
  


  
  
  
```



| Intent | Trigger |
|--------|---------|







  
  
  















---





```

    ↓

    ↓

    ↓

    ↓
┌──────────────────────┐  ┌───────────────────────┐


└──────────────────────┘  └───────────────────────┘
    ↓                          ↓

    ↓──────────────┬────────────↓
                   ↓
           
                   ↓
           
                   ↓

       
```



```

    ↓

    ↓

    ↓

    ↓

    ↓

    ↓

    ↓
ConsolidationAgent:
    
    
    ↓


    ↓

```


---






```python

    
    
    messages (reminders, daily email review, job results) go out through `UserNotificationService`.
    

    
    """
)


```


```python

    


    
```


```python

    config = AgentConfig(
        
    )

    async def can_handle(self, message: AgentMessage) -> bool:
        
        return (
            message.intent == AgentIntent.DELEGATE
            
        )
```


```

  ↓

  ↓

  ↓

  ↓


  ↓

```

---






|------|-------|------|-------|----------------|---------------|










  
  
  
  


---








```

  


  
  
  

Recovery:
  
  
```












**Pattern:**
```



```






---






















**Implementation:**






---

---










---



