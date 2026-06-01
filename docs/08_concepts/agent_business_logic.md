










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
User: "What's today's weather in Valencia?"
  ↓
QuickResponseAgent: Turn 1 — LLM calls search_web_light("Valencia weather today")
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

  → MemorySearchAgent returns: "Honda Civic 2019"
Turn 2: LLM calls ask_web_search_agent("gas prices Valencia")
  

    full_response="You have a Honda Civic 2019...",
    history_summary="Q: car+gas. A: Civic 2019, €1.45/l. 🚗⛽"
  )
  
```








---











```





```

**Example:**
```

  ↓

  ↓
Finds: "User owns a Honda Civic 2019" (score: 0.92)
  ↓

```








---











```





```

**Example:**
```
User: "What's the weather in Valencia today?"
  ↓
WebSearchAgent: Gemini Grounding search("Valencia weather")
  ↓

  ↓
Returns: "Valencia: 22°C, sunny, light breeze"
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
"Honda Civic 2019"       "€1.45/liter in Valencia"
    ↓──────────────┬────────────↓
                   ↓
           
                   ↓
           
                   ↓
User: "You have a Honda Civic 2019. Current gas prices
       in Valencia are around €1.45 per liter."
```



```
User: "I moved to Valencia last month"
    ↓

    ↓

    ↓

    ↓

    ↓

    ↓

    ↓
ConsolidationAgent:
    Extracts: "User lives in Valencia" (high conf)
    
    ↓

Updates old fact: "User lived in Warsaw" → is_current=False
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



