CREATE TABLE [Sales].[SalesTerritory] (
    [TerritoryID] int NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [CountryRegionCode] nvarchar(3) NOT NULL,
    [Group] nvarchar(50) NOT NULL,
    [SalesYTD] money NOT NULL,
    [SalesLastYear] money NOT NULL,
    [CostYTD] money NOT NULL,
    [CostLastYear] money NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
