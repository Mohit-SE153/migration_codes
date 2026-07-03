CREATE TABLE [Sales].[Store] (
    [BusinessEntityID] int NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [SalesPersonID] int NULL,
    [Demographics] xml NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
