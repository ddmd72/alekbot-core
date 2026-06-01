










```

```


```

```



---






|--------|--------|-------------------|








|--------|-----------|-------------------|




---







```python

    ↓

    ↓

    

```






**Code:**
```python
class WebSearchAgent(BaseAgent):
    
        
        self.llm_port = llm_port
        

    async def process(self, message: AgentMessage) -> AgentResponse:
        
        
        return AgentResponse.success(...)
```






```python
SmartResponseAgent
    ↓

    ↓

    

```

**Use Case:**
```



  
  
  
```

**Guards:**




---










**Example:**
```python

    
        
```



---















---









**Example:**
```python

    def __init__(
        self,
        
        
        
        
    ):
        

    async def process(self, message: AgentMessage) -> AgentResponse:
        
        
            

        
        

        
        
            
            

        
        

        
```






---







```python
class BaseAgent(ABC):
    

    
        
            try:
                

                # Validate response
                if response.status == AgentStatus.SUCCESS:
                    return response

                

            except Exception as e:
                
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        
                    )
                

        return AgentResponse.failure(...)
```



```python

response = await self.coordinator.route_message(
    AgentMessage.create(
        sender=self.agent_id,
        recipient="memory_search_agent",
        intent=AgentIntent.DELEGATE,
        
    )
)


    
    response = await self.coordinator.route_message(
        AgentMessage.create(
            sender=self.agent_id,
            
            intent=AgentIntent.DELEGATE,
            
        )
    )
```







```python
class CircuitBreaker:
    
        
        
        

    
        if agent_id not in self._failures:
            return False

        

        # Auto-recovery after timeout
        
            del self._failures[agent_id]
            return False

        

    def record_failure(self, agent_id: str):
        if agent_id in self._failures:
            count, _ = self._failures[agent_id]
            self._failures[agent_id] = (count + 1, time.time())
        else:
            self._failures[agent_id] = (1, time.time())
```

---





```python

    
        self.coordinator = coordinator
        
        

    async def process(self, message: AgentMessage) -> AgentResponse:
        
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                
            )

        
        try:
            

            response = await self.coordinator.route_message(
                AgentMessage.create(
                    sender=self.agent_id,
                    recipient="smart_response_agent",
                    intent=AgentIntent.DELEGATE,
                    payload=message.payload,
                    
                )
            )

            return response
        finally:
            
```



```python

    
        
        
        
    }

    async def process(self, message: AgentMessage) -> AgentResponse:
        
        
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                
            )

        
        
        

        response = await self.coordinator.route_message(
            AgentMessage.create(
                sender=self.agent_id,
                recipient="smart_response_agent",
                intent=AgentIntent.DELEGATE,
                
                
            )
        )

        return response
```

---




```python


```







```python

    
        

        
            
```






---




```python
# BAD

    
```


```python
# BAD

    
    
        AgentMessage.create(
            ...,
            
        )
    )
```


```python
# BAD

    
    
```

---



```



│


│

    
        
        
        
        
```

---




























---









---



